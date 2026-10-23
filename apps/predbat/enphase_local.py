# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application may be used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Enforce Predbat battery windows through the IQ Gateway's local mode override.

Only the component's worker talks to the gateway. Home Assistant callbacks stage
controls and wake that worker, so a slow gateway cannot block HA's event reader.
The endpoint controls modes, not watts, native schedules or hardware reserves.
"""

import asyncio
import copy
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from component_base import ComponentBase
from enphase_local_client import EnphaseLocalClient, EnphaseLocalError, EnphaseLocalSuperseded


TIME_OPTIONS = [f"{minute // 60:02d}:{minute % 60:02d}:00" for minute in range(1440)]
MODE_NAMES = {-1: "Automatic", 1: "Zero Net", 2: "Charge From Grid", 3: "Discharge to Grid", 4: "No Discharge"}
CONTROL_MODES = ("Control charge", "Control charge & discharge")


class EnphaseLocalAPI(ComponentBase):
    """A single-gateway, local scheduler with bounded, verified mode writes."""

    def initialize(self, host, token, fingerprint=None, automatic=False, battery_rate_max=0, capacity_kwh=None, poll_seconds=5, request_timeout=5, allow_discharge=True):
        """Configure the client without making requests or taking battery control."""
        self.host = str(host).strip().rstrip("/")
        self.poll_seconds = float(poll_seconds)
        self.request_timeout = float(request_timeout)
        if not math.isfinite(self.poll_seconds) or not 2 <= self.poll_seconds <= 30:
            raise ValueError("enphase_local_poll_seconds must be between 2 and 30")
        if not math.isfinite(self.request_timeout) or not 1 <= self.request_timeout <= 15:
            raise ValueError("enphase_local_request_timeout must be between 1 and 15")
        self.battery_rate_max = float(battery_rate_max or 0)
        self.capacity_kwh = float(capacity_kwh) if capacity_kwh is not None else None
        if not math.isfinite(self.battery_rate_max) or self.battery_rate_max < 0:
            raise ValueError("enphase_local_battery_rate_max must be a non-negative wattage")
        if self.capacity_kwh is not None and (not math.isfinite(self.capacity_kwh) or self.capacity_kwh <= 0):
            raise ValueError("enphase_local_capacity_kwh must be positive")
        self.automatic = automatic
        self.allow_discharge = allow_discharge
        self.client = EnphaseLocalClient(host, token, fingerprint=fingerprint, timeout=self.request_timeout)
        self._entity = f"{self.prefix}_enphase_local"
        self._lock = threading.Lock()
        self._staged = self._default_schedule()
        self.schedule = None
        self._revision = 0
        self._confirmed_revision = -1
        self._loop = None
        self._wake = None
        self._ready = None
        self._worker = None
        self._owned = False
        self._ownership_loaded = False
        self._recover_owned = False
        self._storage = None
        self._shutdown_task = None
        self._configured = False
        self._planner_services = False
        self._last_error = None
        self._desired_mode = None
        self.state = None
        self._state_at = None
        self._publish_condition = threading.Condition()
        self._publish_stop = threading.Event()
        self._publish_pending = {}
        self._publish_latest = {}
        self._publish_delivered_at = {}
        self._publish_inflight = None
        self._publisher = None

    def dashboard_item(self, entity, state, attributes, app=None):
        """Update local controls immediately and publish outside the gateway/event loops."""
        interface = getattr(self.base, "ha_interface", None)
        if not callable(getattr(interface, "update_state_item", None)):
            # Lightweight standalone test bases have no HA transport or state cache.
            return super().dashboard_item(entity, state, attributes, app=app)
        snapshot = {"state": state, "attributes": copy.deepcopy(attributes)}
        with self._publish_condition:
            if self._publish_stop.is_set():
                return
            item = dict(snapshot, last_changed=datetime.now(timezone.utc).isoformat())
            interface.update_state_item(item, entity, nodb=True)
            if hasattr(interface, "db_mirror_list"):
                interface.db_mirror_list[entity] = True
            if hasattr(self.base, "dashboard_values"):
                self.base.dashboard_values[entity] = copy.deepcopy(snapshot)
            if app and hasattr(self.base, "dashboard_index_app"):
                self.base.dashboard_index_app[entity] = app
            unchanged = self._publish_latest.get(entity) == snapshot
            if unchanged and (entity in self._publish_pending or self._publish_inflight == entity or time.monotonic() - self._publish_delivered_at.get(entity, 0) < 60):
                return
            self._publish_latest[entity] = snapshot
            self._publish_pending[entity] = snapshot
            if self._publisher is None:
                self._publisher = threading.Thread(target=self._publish_worker, name="enphase-local-publisher", daemon=True)
                self._publisher.start()
            self._publish_condition.notify()

    def _publish_worker(self):
        """Coalesce entity changes in one thread so slow HA requests cannot stall control."""
        failed_before = False
        while not self._publish_stop.is_set():
            with self._publish_condition:
                while not self._publish_pending and not self._publish_stop.is_set():
                    self._publish_condition.wait()
                if self._publish_stop.is_set():
                    return
                entity = next(iter(self._publish_pending))
                snapshot = self._publish_pending.pop(entity)
                self._publish_inflight = entity
            failed = False
            try:
                interface = self.base.ha_interface
                if interface.db_enable and (interface.db_mirror_ha or interface.db_primary):
                    interface.db_manager.set_state_db(entity, snapshot["state"], snapshot["attributes"])
                if interface.ha_key and not self._publish_stop.is_set():
                    # No GET/readback: an older HTTP completion must not replace
                    # the newer local control state used by write_and_poll.
                    response = interface.api_call(f"/api/states/{entity}", dict(snapshot, unrecorded_attributes=["results"]), post=True)
                    failed = not isinstance(response, dict) or response.get("entity_id") != entity
            except Exception as error:
                failed = True
                if not failed_before:
                    self.log(f"Warn: EnphaseLocal: HA status publication failed ({type(error).__name__}); retrying separately from battery control")
            with self._publish_condition:
                self._publish_inflight = None
                if failed:
                    if not self._publish_stop.is_set() and self._publish_latest.get(entity) == snapshot:
                        self._publish_pending.setdefault(entity, snapshot)
                elif self._publish_latest.get(entity) == snapshot:
                    self._publish_delivered_at[entity] = time.monotonic()
            if failed:
                if not failed_before:
                    self.log("Warn: EnphaseLocal: HA status publication pending; battery control continues independently")
                self._publish_stop.wait(self.poll_seconds)
            failed_before = failed

    @staticmethod
    def _default_schedule():
        """Return disabled windows; never restore yesterday's daily force windows."""
        return {"reserve": 5, "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False}}

    @staticmethod
    def _valid_soc(value):
        """Reject absent, non-finite and out-of-range SOC instead of inventing zero."""
        if isinstance(value, bool) or value is None:
            raise ValueError("Gateway battery SOC is unavailable")
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("Gateway battery SOC is outside 0-100%")
        return value

    @staticmethod
    def in_window(minutes_now, window):
        """Check an enabled window, including midnight wrap and exclusive end."""
        if not window.get("enable"):
            return False
        start = datetime.strptime(window.get("start_time", "00:00:00"), "%H:%M:%S")
        end = datetime.strptime(window.get("end_time", "00:00:00"), "%H:%M:%S")
        start = start.hour * 60 + start.minute
        end = end.hour * 60 + end.minute
        if start == end:
            return False
        return start <= minutes_now < end if start < end else minutes_now >= start or minutes_now < end

    def evaluate_schedule(self, minutes_now, soc, schedule=None):
        """Choose a mode from live SOC and committed windows; never command watts."""
        soc = self._valid_soc(soc)
        schedule = schedule if schedule is not None else self.schedule or self._default_schedule()
        reserve = self._valid_soc(schedule.get("reserve", 5))
        charge, export = schedule.get("charge", {}), schedule.get("export", {})
        if self.in_window(minutes_now, charge):
            return 2 if soc < self._valid_soc(charge.get("soc", 100)) else 4
        if self.in_window(minutes_now, export):
            target = self._valid_soc(export.get("soc", 100))
            # 99/100 are Predbat's freeze/disabled sentinels, not export targets.
            # Freeze Export stops CHARGING: No Discharge is not that operation.
            if schedule.get("export_target_valid", True) and target < 99 and self.allow_discharge and soc > max(target, reserve):
                return 3
        return 4 if soc <= reserve else 1

    def get_minutes_now(self):
        """Use the live local clock rather than the last five-minute planning tick."""
        now = datetime.now(timezone.utc).astimezone(self.local_tz)
        return now.hour * 60 + now.minute

    def _requested_mode(self, soc, schedule):
        """Apply Predbat's mode permissions to the current window/SOC decision."""
        desired = self.evaluate_schedule(self.get_minutes_now(), soc, schedule)
        if desired == 3 and self.get_arg("mode", "Monitor") != "Control charge & discharge":
            return 4 if soc <= schedule["reserve"] else 1
        return desired

    def _wake_worker(self):
        """Wake the owner loop from an HA callback without doing HTTP on its loop."""
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass  # shutdown won the race; never fall back to inline HTTP

    def _field(self, entity_id):
        """Return the control suffix only for this component's own entities."""
        _, separator, name = entity_id.partition(".")
        prefix = f"{self._entity}_battery_schedule_"
        return name[len(prefix) :] if separator and name.startswith(prefix) else None

    async def select_event(self, entity_id, value):
        """Stage a time selection; committing it performs no network operation."""
        field = self._field(entity_id)
        if value not in TIME_OPTIONS:
            return
        with self._lock:
            for side in ("charge", "export"):
                for key in ("start_time", "end_time"):
                    if field == f"{side}_{key}":
                        if side == "export" and self._planner_services and self._staged[side][key] != value:
                            self._staged["export_target_valid"] = False
                        self._staged[side][key] = value
        self.publish_schedule_settings_ha()

    async def number_event(self, entity_id, value):
        """Stage targets; commit a reserve guard immediately without applying windows."""
        field = self._field(entity_id)
        try:
            value = self._valid_soc(value)
        except (ValueError, TypeError):
            return
        with self._lock:
            if field == "reserve":
                self._staged["reserve"] = value
                committed = copy.deepcopy(self.schedule or self._default_schedule())
                committed["reserve"] = value
                self.schedule = committed
                self._revision += 1
            for side in ("charge", "export"):
                if field == f"{side}_soc":
                    self._staged[side]["soc"] = value
                    if side == "export":
                        self._staged["export_target_valid"] = True
        self._wake_worker()
        self.publish_schedule_settings_ha()

    async def switch_event(self, entity_id, service):
        """Stage an enable or commit a snapshot, returning to HA before device IO."""
        field = self._field(entity_id)
        if service not in ("turn_on", "turn_off", "toggle"):
            return
        with self._lock:
            for side in ("charge", "export"):
                if field == f"{side}_enable":
                    old = self._staged[side]["enable"]
                    self._staged[side]["enable"] = service == "turn_on" or (service == "toggle" and not old)
            if field in ("charge_write", "export_write") and service != "turn_off":
                self.schedule = copy.deepcopy(self._staged)
                self._revision += 1
        self._wake_worker()
        self.publish_schedule_settings_ha()

    def _read_only(self):
        """Honor monitor/SOC-only modes and the live read-only configuration."""
        return bool(self.get_arg("set_read_only", False)) or self.get_state_wrapper(f"switch.{self.prefix}_set_read_only", "off") == "on" or self.get_arg("mode", "Monitor") not in CONTROL_MODES

    def _cloud_conflict(self):
        """Reject concurrent cloud control even when cloud automatic-config is disabled."""
        components = getattr(self.base, "components", None)
        return bool(components and components.get_component("enphase"))

    async def _load_ownership(self):
        """Recover only ownership, never old windows that could restart forced charging."""
        if self._ownership_loaded:
            return
        self._storage = self._storage or self.storage
        data = await self._storage.load("enphase_local", "ownership") if self._storage else None
        if isinstance(data, dict) and data.get("owned") and data.get("host") == self.host:
            self._owned = True
            self._recover_owned = True
        self._ownership_loaded = True

    async def _save_ownership(self, owned):
        """Persist a recovery marker before taking control of the gateway."""
        self._storage = self._storage or self.storage
        if not self._storage:
            raise EnphaseLocalError("Storage is unavailable; cannot retain gateway override ownership")
        result = await self._storage.save("enphase_local", "ownership", {"owned": owned, "host": self.host}, format="json", expiry=None)
        if result is False:
            raise EnphaseLocalError("Could not save gateway override ownership")

    async def _release_override(self):
        """Release only an override this component owns, verifying the gateway response."""
        if not self._owned:
            return
        state = await self.client.set_mode(-1)
        if state.get("override_mode") != -1:
            raise EnphaseLocalError("Gateway did not confirm release of the local override")
        self.state = state
        await self._save_ownership(False)
        self._owned = False
        self._recover_owned = False

    def _set_error(self, message):
        """Keep failed/pending control visible, logging a repeated failure only once."""
        if message != self._last_error:
            self.log(f"Warn: EnphaseLocal: {message}")
        self._last_error = message
        self.count_errors += 1

    async def control_once(self):
        """Read fresh SOC, evaluate the latest snapshot and verify any mode change."""
        try:
            await self._load_ownership()
            if self._recover_owned:
                await self._release_override()
            if self._cloud_conflict():
                await self._release_override()
                raise EnphaseLocalError("Disable the Enphase cloud component before using local battery control")
            if self.get_arg("set_charge_low_power", False) or self.get_arg("set_export_low_power", False):
                raise EnphaseLocalError("Disable set_charge_low_power and set_export_low_power: the local API has no wattage setpoint")
            state = await self.client.read_state()
            soc = self._valid_soc(state.get("soc_percent"))
            self.state = state
            self._state_at = datetime.now(timezone.utc)
            if self.automatic and not self._configured:
                await self.automatic_config()
                self._configured = True
            with self._lock:
                revision = self._revision
                schedule = copy.deepcopy(self.schedule)
            if self._read_only() or self.api_stop or self.fatal_error or schedule is None:
                await self._release_override()
                self._desired_mode = None
                self._confirmed_revision = revision if schedule is None else -1
            else:
                desired = self._requested_mode(soc, schedule)
                self._desired_mode = desired
                if not self._owned:
                    # Claim durably before POST: a crash after acceptance must be recoverable.
                    await self._save_ownership(True)
                    self._owned = True
                with self._lock:
                    superseded = self._revision != revision
                if superseded:
                    self._wake_worker()
                    return False
                if state.get("override_mode") != desired:

                    def still_current(fresh):
                        """Recheck SOC, clock and control permission immediately before POST."""
                        with self._lock:
                            current = self._revision == revision
                        return (
                            current
                            and not self._read_only()
                            and not self.api_stop
                            and not self.fatal_error
                            and not self.get_arg("set_charge_low_power", False)
                            and not self.get_arg("set_export_low_power", False)
                            and self._requested_mode(self._valid_soc(fresh.get("soc_percent")), schedule) == desired
                        )

                    confirmed = await self.client.set_mode(desired, guard=still_current)
                    if confirmed.get("override_mode") != desired:
                        raise EnphaseLocalError("Gateway readback does not match the requested mode")
                    self._valid_soc(confirmed.get("soc_percent"))
                    self.state = confirmed
                    self._state_at = datetime.now(timezone.utc)
                with self._lock:
                    target_ready = schedule.get("export_target_valid", True) or not self.in_window(self.get_minutes_now(), schedule["export"])
                    if self._revision == revision and target_ready:
                        self._confirmed_revision = revision
                    else:
                        self._wake_worker()
            self._last_error = None
            self.update_success_timestamp()
            self.publish_data()
            self.publish_schedule_settings_ha()
            return True
        except EnphaseLocalSuperseded as changed:
            self.state = changed.state
            self._state_at = datetime.now(timezone.utc)
            self._confirmed_revision = -1
            self._wake_worker()
            self.publish_data()
            self.publish_schedule_settings_ha()
            return False
        except (EnphaseLocalError, ValueError, TypeError, OSError, RuntimeError, asyncio.TimeoutError) as error:
            self._state_at = None
            # Missing fresh SOC must never leave our last forced charge/export running
            # merely because the planner can no longer decide which limit applies.
            try:
                await self._release_override()
            except (EnphaseLocalError, OSError, RuntimeError, asyncio.TimeoutError) as release_error:
                self.log(f"Warn: EnphaseLocal: override release remains pending: {type(release_error).__name__}")
            self._set_error(str(error) or type(error).__name__)
            self.publish_data()
            self.publish_schedule_settings_ha()
            return False
        finally:
            if self._ready:
                self._ready.set()

    async def _control_worker(self):
        """Own all device IO and coalesce wakeups instead of replaying stale commands."""
        while not self.api_stop and not self.fatal_error:
            self._wake.clear()
            await self.control_once()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def run(self, seconds, first):
        """Start the fast owner-loop worker; ComponentBase retains lifecycle control."""
        if self._worker is None or self._worker.done():
            self._loop = asyncio.get_running_loop()
            self._wake = asyncio.Event()
            self._ready = asyncio.Event()
            self._worker = asyncio.create_task(self._control_worker())
        await asyncio.wait_for(self._ready.wait(), timeout=self.request_timeout * 4 + 5)
        return self._state_at is not None and self._last_error is None

    async def stop(self):
        """Finish cleanup on the owner loop before a replacement component can start."""
        self.api_stop = True
        self.api_started = False
        self._wake_worker()
        if self._loop is not None and not self._loop.is_closed() and self._loop is not asyncio.get_running_loop():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            await asyncio.wrap_future(future)
        else:
            await self._shutdown()

    async def final(self):
        """Join the same idempotent cleanup used by stop, on the component's loop."""
        await self._shutdown()

    async def _shutdown(self):
        """Coalesce stop/final calls so an old instance cannot release a new one's mode."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._shutdown_task)

    async def _cleanup(self):
        """Cancel worker IO, release owned override with a deadline and close the client."""
        self._publish_stop.set()
        with self._publish_condition:
            self._publish_pending.clear()
            self._publish_condition.notify_all()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.wait_for(self._release_override(), timeout=self.request_timeout * 2 + 1)
        except (EnphaseLocalError, OSError, RuntimeError, asyncio.TimeoutError) as error:
            self._set_error(f"Override release remains pending after shutdown ({type(error).__name__}); check the gateway")
        finally:
            await self.client.close()

    def is_alive(self):
        """Report fresh gateway contact, rather than discovery or an old write acknowledgement."""
        age_limit = timedelta(seconds=self.poll_seconds * 2 + self.request_timeout * 4)
        return bool(self.api_started and self._state_at and datetime.now(timezone.utc) - self._state_at < age_limit and not self._last_error)

    def health_message(self):
        """Expose the concrete pending-control or connection problem on component health."""
        return self._last_error

    def publish_data(self):
        """Publish only verified local fields; retain HA power and energy-history inputs."""
        state = self.state or {}
        available = self._state_at is not None
        values = {
            "soc_percent": (state.get("soc_percent"), "%", "Enphase Local Battery SOC"),
            "battery_capacity": (self.capacity_kwh or state.get("capacity_kwh"), "kWh", "Enphase Local Battery Capacity"),
            "battery_rate_max": (self.battery_rate_max or None, "W", "Enphase Local Configured Maximum Battery Rate"),
        }
        for name, (value, unit, label) in values.items():
            attributes = {"friendly_name": label, "unit_of_measurement": unit}
            self.dashboard_item(f"sensor.{self._entity}_{name}", value if available and value is not None else "unavailable", attributes, app="enphase_local")
        for field, suffix in (("override_mode", "override_mode"), ("operating_mode", "operating_mode")):
            value = state.get(field)
            self.dashboard_item(
                f"sensor.{self._entity}_{suffix}", value if available and value is not None else "unavailable", {"friendly_name": f"Enphase Local {suffix.replace('_', ' ').title()}", "mode_name": MODE_NAMES.get(value, "Unknown")}, app="enphase_local"
            )
        self.dashboard_item(f"sensor.{self._entity}_inverter_time", self._state_at.astimezone(self.local_tz).strftime("%Y-%m-%d %H:%M:%S") if available else "unavailable", {"friendly_name": "Enphase Local Last Gateway Read"}, app="enphase_local")
        expiry = getattr(self.client, "token_expires_at", None)
        self.dashboard_item(f"sensor.{self._entity}_token_expiry", datetime.fromtimestamp(expiry, timezone.utc).isoformat() if expiry else "unknown", {"friendly_name": "Enphase Local Token Expiry", "device_class": "timestamp"}, app="enphase_local")

    def publish_schedule_settings_ha(self):
        """Separate staged intent from verified control and reset momentary commit buttons."""
        with self._lock:
            staged = copy.deepcopy(self._staged)
            revision = self._revision
        entity = f"{self._entity}_battery_schedule"
        self.dashboard_item(f"number.{entity}_reserve", staged["reserve"], {"friendly_name": "Enphase Local Software Reserve", "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"}, app="enphase_local")
        for side in ("charge", "export"):
            window = staged[side]
            for key in ("start_time", "end_time"):
                self.dashboard_item(f"select.{entity}_{side}_{key}", window[key], {"friendly_name": f"Enphase Local {side} {key}", "options": TIME_OPTIONS}, app="enphase_local")
            self.dashboard_item(f"number.{entity}_{side}_soc", window["soc"], {"friendly_name": f"Enphase Local {side} Target", "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"}, app="enphase_local")
            for key, on in (("enable", window["enable"]), ("write", False)):
                self.dashboard_item(f"switch.{entity}_{side}_{key}", "on" if on else "off", {"friendly_name": f"Enphase Local {side} {key}"}, app="enphase_local")
        ok = self._last_error is None and self._confirmed_revision == revision and self._state_at is not None
        self.dashboard_item(
            f"binary_sensor.{self._entity}_schedule_write_ok",
            "on" if ok else "off",
            {
                "friendly_name": "Enphase Local Control Verified",
                "pending": self._confirmed_revision != revision,
                "control_enabled": not self._read_only(),
                "owned": self._owned,
                "desired_mode": self._desired_mode,
                "confirmed_mode": (self.state or {}).get("override_mode"),
                "error": self._last_error,
            },
            app="enphase_local",
        )

    async def automatic_config(self):
        """Bind local battery controls, preserving separately configured household telemetry."""
        if self._cloud_conflict():
            raise ValueError("Disable the Enphase cloud component before configuring local control")
        if not self.state or not (self.capacity_kwh or self.state.get("capacity_kwh")):
            raise ValueError("Gateway capacity unavailable; configure enphase_local_capacity_kwh")
        if not self.battery_rate_max:
            raise ValueError("Configure enphase_local_battery_rate_max in watts for this battery")
        # Changing an inverter binding must not keep sending rate/service writes to
        # the previously configured controller. These are controls, not telemetry.
        for name in (
            "charge_rate",
            "charge_rate_percent",
            "discharge_rate",
            "discharge_rate_percent",
            "timed_charge_current",
            "timed_discharge_current",
            "charge_limit_enable",
            "charge_start_service",
            "charge_stop_service",
            "charge_freeze_service",
            "discharge_freeze_service",
        ):
            self.set_arg(name, None)
        entity = self._entity
        self.set_arg("inverter_type", ["EnphaseLocal"])
        self.set_arg("num_inverters", 1)
        for name, suffix in (("soc_percent", "soc_percent"), ("soc_max", "battery_capacity"), ("battery_rate_max", "battery_rate_max"), ("inverter_time", "inverter_time")):
            self.set_arg(name, [f"sensor.{entity}_{suffix}"])
        schedule = f"{entity}_battery_schedule"
        self.set_arg("reserve", [f"number.{schedule}_reserve"])
        for side, generic in (("charge", "charge"), ("export", "discharge")):
            self.set_arg(f"{generic}_start_time", [f"select.{schedule}_{side}_start_time"])
            self.set_arg(f"{generic}_end_time", [f"select.{schedule}_{side}_end_time"])
            if side == "charge":
                self.set_arg("charge_limit", [f"number.{schedule}_{side}_soc"])
            self.set_arg(f"scheduled_{generic}_enable", [f"switch.{schedule}_{side}_enable"])
        self.set_arg("schedule_write_button", [f"switch.{schedule}_charge_write"])
        # adjust_force_export writes the inverter's MINIMUM reserve here. The
        # plan's actual export stop comes through adjust_export_immediate's
        # target_soc service argument, independently of reserve-control settings.
        self.set_arg("discharge_target_soc", [f"number.{schedule}_reserve"])
        self.set_arg(
            "discharge_start_service",
            [
                {"service": "switch.turn_on", "entity_id": f"switch.{schedule}_export_enable"},
                {"service": "number.set_value", "entity_id": f"number.{schedule}_export_soc", "value": "{target_soc}"},
                {"service": "switch.turn_on", "entity_id": f"switch.{schedule}_export_write"},
            ],
        )
        self.set_arg(
            "discharge_stop_service",
            [
                {"service": "switch.turn_off", "entity_id": f"switch.{schedule}_export_enable"},
                {"service": "switch.turn_on", "entity_id": f"switch.{schedule}_export_write"},
            ],
        )
        self._planner_services = True
        with self._lock:
            self._staged["export_target_valid"] = False
