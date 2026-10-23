# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# -----------------------------------------------------------------------------
"""Offline tests for locally enforced Enphase battery schedules and ownership."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import threading
import time

import pytz

from enphase_local import EnphaseLocalAPI
from mock_base import MockBase
from tests.test_infra import run_async


class LocalStorage:
    """Retain component state in memory and record its ordering against mode writes."""

    def __init__(self, operations):
        """Create an empty store sharing the client's operation history."""
        self.operations = operations
        self.data = {}
        self.fail_save = False

    async def load(self, module, filename, **kwargs):
        """Return an isolated copy of previously persisted state."""
        return copy.deepcopy(self.data.get((module, filename)))

    async def save(self, module, filename, data, **kwargs):
        """Record a durable save, or simulate its explicit failure."""
        self.operations.append(("save", copy.deepcopy(data)))
        if self.fail_save:
            return False
        self.data[(module, filename)] = copy.deepcopy(data)
        return True


class LocalComponents:
    """Expose an in-memory Storage component and optional competing cloud controller."""

    def __init__(self, storage):
        """Register storage without enabling any inverter component."""
        self.components = {"storage": storage}

    def get_component(self, name):
        """Find a component using the same lookup as ComponentBase.storage."""
        return self.components.get(name)

    def is_active(self, name):
        """Report whether a component instance is currently registered."""
        return self.components.get(name) is not None


class LocalBase(MockBase):
    """Capture entities and logs while preserving the actual ComponentBase interface."""

    def __init__(self, operations):
        """Create a fully controlled local environment without any network service."""
        super().__init__(local_tz=pytz.timezone("Europe/London"))
        self.logs = []
        self.args["mode"] = "Control charge & discharge"
        self.components = LocalComponents(LocalStorage(operations))

    def log(self, message, quiet=True):
        """Keep log messages available for assertions without printing test credentials."""
        self.logs.append(str(message))

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Store published controls and confirmed status separately."""
        self.set_state_wrapper(entity_id, state, attributes, app=app)

    def set_arg(self, key, value):
        """Apply automatic configuration to the in-memory argument dictionary."""
        if value is None:
            self.args.pop(key, None)
        else:
            self.args[key] = value

    def get_arg(self, key, default=None, indirect=True, index=None, **kwargs):
        """Resolve indexed control bindings as the real Inverter methods expect."""
        value = self.args.get(key, default)
        if isinstance(value, list) and index is not None:
            value = value[index] if index < len(value) else default
        if indirect and isinstance(value, str) and value.startswith(("sensor.", "number.", "switch.", "select.")):
            return self.get_state_wrapper(value, default)
        return value


class LocalClient:
    """Return gateway states and simulate transport outcomes without HTTP."""

    def __init__(self, operations):
        """Start in released self-consumption with a valid measured SOC."""
        self.operations = operations
        self.state = {"override_mode": -1, "operating_mode": 1, "soc_percent": 50.0, "capacity_kwh": 15.0, "mode_names": {1: "ZeroNet", 2: "Charge", 3: "Discharge", 4: "NoDischarge"}}
        self.mode_calls = []
        self.read_calls = 0
        self.read_error = None
        self.mode_error = None
        self.read_entered = None
        self.read_gate = None
        self.preflight_state = None
        self.before_mode = None
        self.release_gate = None
        self.release_signal = None
        self.closed = False
        self.close_calls = 0

    async def read_state(self):
        """Read fresh state, optionally waiting so callback concurrency can be tested."""
        self.read_calls += 1
        if self.read_entered is not None:
            self.read_entered.set()
        if self.read_gate is not None:
            await self.read_gate.wait()
        if self.read_error is not None:
            raise self.read_error
        return copy.deepcopy(self.state)

    async def set_mode(self, mode, guard=None):
        """Guard a fresh preflight state before recording an actual gateway write."""
        if self.before_mode is not None:
            self.before_mode()
        if mode != -1:
            self.state = copy.deepcopy(self.preflight_state if self.preflight_state is not None else self.state)
            if guard is not None and not guard(copy.deepcopy(self.state)):
                from enphase_local_client import EnphaseLocalSuperseded

                raise EnphaseLocalSuperseded(copy.deepcopy(self.state))
        self.mode_calls.append(mode)
        self.operations.append(("mode", mode))
        if mode == -1:
            if self.release_signal is not None:
                self.release_signal.set()
            if self.release_gate is not None:
                await self.release_gate.wait()
        if self.mode_error is not None:
            raise self.mode_error
        self.state["override_mode"] = mode
        self.state["operating_mode"] = 1 if mode == -1 else mode
        return copy.deepcopy(self.state)

    async def close(self):
        """Record transport cleanup."""
        self.closed = True
        self.close_calls += 1


class BlockingLocalHA:
    """Keep the immediate HA cache usable while a background HTTP publication is blocked."""

    def __init__(self, base):
        """Provide the real HA publisher interface with a bounded test-owned network gate."""
        self.state_data = base.entities
        self.db_enable = False
        self.db_mirror_ha = False
        self.db_primary = False
        self.db_mirror_list = {}
        self.ha_key = "test-only"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def update_state_item(self, item, entity_id, nodb=False):
        """Apply local state immediately without waiting for the fake HTTP request."""
        self.state_data[entity_id] = copy.deepcopy(item)

    def api_call(self, endpoint, data_in=None, post=False, core=True, silent=False):
        """Delay a response that deliberately contains the older submitted snapshot."""
        snapshot = copy.deepcopy(data_in)
        self.calls.append((endpoint, snapshot, post))
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise RuntimeError("test HA response gate was not released")
        return dict(snapshot, entity_id=endpoint.split("/api/states/", 1)[1])


def make_local(**kwargs):
    """Construct the real component with mocked transport and Storage dependencies."""
    operations = []
    base = LocalBase(operations)
    defaults = {"host": "https://gateway.invalid", "token": "test-only-token", "battery_rate_max": 3500, "capacity_kwh": 15}
    defaults.update(kwargs)
    api = EnphaseLocalAPI(base, **defaults)
    api.client = LocalClient(operations)
    return api


def window(start="09:00:00", end="11:00:00", soc=80, enabled=True):
    """Build one generic Predbat window without applying it."""
    return {"start_time": start, "end_time": end, "soc": soc, "enable": enabled}


def schedule(charge=None, export=None, reserve=5):
    """Create a complete independent desired schedule."""
    return {"reserve": reserve, "charge": charge or window(enabled=False), "export": export or window(soc=20, enabled=False)}


def active_window(api, soc=80):
    """Keep control-loop tests inside a window regardless of the machine's clock."""
    now = datetime.now(api.local_tz)
    start = (now - timedelta(minutes=10)).strftime("%H:%M:00")
    end = (now + timedelta(minutes=10)).strftime("%H:%M:00")
    return window(start, end, soc)


def install_schedule(api, desired):
    """Install a committed revision as if the planner had pressed its write switch."""
    api.schedule = copy.deepcopy(desired)
    api._staged = copy.deepcopy(desired)
    api._revision += 1


def entity(api, field):
    """Resolve one staged control entity for the component's configured prefix."""
    domain = "select" if field.endswith("_time") else "switch" if field.endswith(("_enable", "_write")) else "number"
    return f"{domain}.{api.prefix}_enphase_local_battery_schedule_{field}"


def test_local_window_boundaries():
    """Start is inclusive, end exclusive, and equal endpoints mean no active window."""
    current = window()
    assert not EnphaseLocalAPI.in_window(539, current)
    assert EnphaseLocalAPI.in_window(540, current)
    assert EnphaseLocalAPI.in_window(659, current)
    assert not EnphaseLocalAPI.in_window(660, current)
    assert not EnphaseLocalAPI.in_window(600, window("10:00:00", "10:00:00"))


def test_local_window_wraps_midnight():
    """A midnight-crossing interval remains active on both sides of midnight."""
    current = window("22:00:00", "02:00:00")
    assert EnphaseLocalAPI.in_window(1320, current)
    assert EnphaseLocalAPI.in_window(1439, current)
    assert EnphaseLocalAPI.in_window(0, current)
    assert EnphaseLocalAPI.in_window(119, current)
    assert not EnphaseLocalAPI.in_window(120, current)
    assert not EnphaseLocalAPI.in_window(720, current)


def test_local_charge_target_stops_forcing_charge():
    """Charge is active below target and becomes a no-discharge hold at its target."""
    api = make_local()
    desired = schedule(charge=window(soc=80))
    assert api.evaluate_schedule(600, 50, desired) == 2
    assert api.evaluate_schedule(600, 80, desired) == 4
    assert api.evaluate_schedule(600, 81, desired) == 4
    assert api.evaluate_schedule(660, 50, desired) == 1


def test_local_export_target_and_reserve():
    """Forced discharge ends at its target and never proceeds through the reserve."""
    api = make_local()
    desired = schedule(export=window(soc=20), reserve=5)
    assert api.evaluate_schedule(600, 50, desired) == 3
    assert api.evaluate_schedule(600, 20, desired) == 1
    assert api.evaluate_schedule(600, 5, desired) == 4
    desired["reserve"] = 30
    assert api.evaluate_schedule(600, 31, desired) == 3
    assert api.evaluate_schedule(600, 30, desired) == 4


def test_local_charge_wins_over_overlapping_export():
    """Conflicting staged windows cannot force simultaneous charge and discharge."""
    api = make_local()
    desired = schedule(charge=window(soc=80), export=window(soc=20))
    assert api.evaluate_schedule(600, 50, desired) == 2


def test_local_does_not_claim_unsupported_export_freeze():
    """The planner's 99/100 sentinels never become a forced-discharge or no-discharge command."""
    api = make_local()
    for target in (99, 100):
        assert api.evaluate_schedule(600, 50, schedule(export=window(soc=target))) == 1


def test_local_discharge_can_be_disabled():
    """A user can retain charging control while preventing forced export."""
    api = make_local(allow_discharge=False)
    assert api.evaluate_schedule(600, 50, schedule(export=window(soc=20))) == 1


def test_local_idle_respects_reserve():
    """Idle self-consumption changes to no-discharge at or below the software reserve."""
    api = make_local()
    assert api.evaluate_schedule(600, 50, schedule(reserve=10)) == 1
    assert api.evaluate_schedule(600, 10, schedule(reserve=10)) == 4
    assert api.evaluate_schedule(600, 9, schedule(reserve=10)) == 4


def test_local_invalid_soc_is_rejected():
    """Unknown, non-finite and out-of-range SOC cannot select an active battery mode."""
    api = make_local()
    for soc in (None, "unavailable", float("nan"), float("inf"), -1, 101):
        try:
            api.evaluate_schedule(600, soc, schedule(charge=window()))
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"Accepted unsafe SOC: {soc!r}")


def test_local_callbacks_stage_and_commit_independent_snapshots():
    """Callbacks perform no I/O and a later edit cannot mutate an already committed plan."""

    async def check():
        api = make_local()
        await api.select_event(entity(api, "charge_start_time"), "10:00:00")
        await api.number_event(entity(api, "charge_soc"), 75)
        await api.switch_event(entity(api, "charge_enable"), "turn_on")
        assert api.schedule is None
        assert api._staged["charge"]["start_time"] == "10:00:00"
        assert api._staged["charge"]["soc"] == 75
        assert api._staged["charge"]["enable"]
        await api.switch_event(entity(api, "charge_write"), "turn_on")
        committed = copy.deepcopy(api.schedule)
        revision = api._revision
        await api.select_event(entity(api, "charge_start_time"), "11:00:00")
        assert api.schedule == committed
        assert api._revision == revision
        assert api._staged["charge"]["start_time"] == "11:00:00"
        assert api.client.read_calls == 0 and api.client.mode_calls == []

    run_async(check())


def test_local_reserve_commit_does_not_commit_staged_windows():
    """An immediate reserve update preserves the previously committed window snapshot."""

    async def check():
        api = make_local()
        await api.select_event(entity(api, "charge_start_time"), "10:00:00")
        await api.switch_event(entity(api, "charge_enable"), "turn_on")
        await api.number_event(entity(api, "reserve"), 15)
        assert api.schedule["reserve"] == 15
        assert not api.schedule["charge"]["enable"]
        assert api._staged["charge"]["enable"]
        assert api.client.read_calls == 0 and not api.client.mode_calls

    run_async(check())


def test_local_confirmed_control_persists_ownership_before_mode_write():
    """The component only reports success after fresh state and an owned, verified override."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        assert api.client.read_calls >= 1
        assert api.client.mode_calls == [2]
        assert api._owned
        assert api._confirmed_revision == api._revision
        operations = api.client.operations
        write_index = operations.index(("mode", 2))
        ownership_saves = [i for i, (kind, value) in enumerate(operations) if kind == "save" and isinstance(value, dict) and value.get("owned") is True]
        assert ownership_saves and min(ownership_saves) < write_index
        health = api.base.entities[f"binary_sensor.{api.prefix}_enphase_local_schedule_write_ok"]
        assert health["state"] == "on"

    run_async(check())


def test_local_reapplies_when_gateway_override_drifted():
    """A matching cached revision cannot hide an externally changed gateway override."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        api.client.state["override_mode"] = 1
        await api.control_once()
        assert api.client.mode_calls == [2, 2]

    run_async(check())


def test_local_monitor_and_soc_only_modes_never_force():
    """Monitoring and unsupported SOC-only operation leave external overrides untouched."""

    async def check():
        for mode in ("Monitor", "Control SOC only"):
            api = make_local()
            api.base.args["mode"] = mode
            api.client.state["override_mode"] = 3
            install_schedule(api, schedule(charge=active_window(api)))
            await api.control_once()
            assert api.client.mode_calls == [], mode
            assert not api._owned
            assert api._confirmed_revision != api._revision
            health = api.base.entities[f"binary_sensor.{api.prefix}_enphase_local_schedule_write_ok"]
            assert health["state"] == "off"
            assert health["attributes"]["control_enabled"] is False

    run_async(check())


def test_local_charge_only_mode_never_forces_export():
    """Predbat's charge-only mode prevents an export window from selecting discharge."""

    async def check():
        api = make_local()
        api.base.args["mode"] = "Control charge"
        install_schedule(api, schedule(export=active_window(api, soc=20)))
        await api.control_once()
        assert 3 not in api.client.mode_calls

    run_async(check())


def test_local_read_only_startup_does_not_change_external_mode():
    """Read-only startup cannot release or overwrite an override owned by another client."""

    async def check():
        api = make_local()
        api.base.args["set_read_only"] = True
        api.client.state["override_mode"] = 3
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        assert api.client.mode_calls == []
        assert not api._owned
        assert api._confirmed_revision != api._revision
        health = api.base.entities[f"binary_sensor.{api.prefix}_enphase_local_schedule_write_ok"]
        assert health["state"] == "off"
        assert health["attributes"]["control_enabled"] is False

    run_async(check())


def test_local_read_only_transition_releases_only_its_own_override():
    """Switching to read-only relinquishes an override previously claimed by this component."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        api.base.args["set_read_only"] = True
        await api.control_once()
        assert api.client.mode_calls == [2, -1]
        assert not api._owned
        assert api._confirmed_revision != api._revision

    run_async(check())


def test_local_failed_read_only_release_preserves_pending_ownership():
    """Two failed release attempts cannot erase ownership or mark a read-only plan as applied."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        api.base.args["set_read_only"] = True
        api.client.mode_error = RuntimeError("release acknowledgement unavailable")
        await api.control_once()
        assert api.client.mode_calls == [2, -1, -1]
        assert api._owned
        assert any(data.get("owned") is True for data in api.storage.data.values())
        health = api.base.entities[f"binary_sensor.{api.prefix}_enphase_local_schedule_write_ok"]
        assert health["state"] == "off"
        assert health["attributes"]["control_enabled"] is False
        assert api.health_message()

    run_async(check())


def test_local_ui_read_only_prevents_forcing():
    """The dashboard's read-only switch has the same effect as the configuration flag."""

    async def check():
        api = make_local()
        api.base.set_state_wrapper(f"switch.{api.prefix}_set_read_only", "on")
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        assert not api.client.mode_calls

    run_async(check())


def test_local_no_schedule_does_not_take_over_the_gateway():
    """Fresh telemetry alone is not permission to replace a gateway override."""

    async def check():
        api = make_local()
        api.client.state["override_mode"] = 4
        await api.control_once()
        assert api.schedule is None
        assert api.client.mode_calls == []

    run_async(check())


def test_local_does_not_force_from_cached_soc_after_a_failed_read():
    """Old valid telemetry cannot justify forcing charge when the gateway stops answering."""

    async def check():
        api = make_local()
        api.state = copy.deepcopy(api.client.state)
        api._state_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        install_schedule(api, schedule(charge=active_window(api)))
        api.client.read_error = RuntimeError("gateway unavailable")
        await api.control_once()
        assert api.client.mode_calls == []
        assert api._confirmed_revision != api._revision

    run_async(check())


def test_local_invalid_fresh_soc_does_not_force_charge():
    """The component defensively rejects invalid state even when a transport mock returns it."""

    async def check():
        for soc in (None, float("nan"), -1, 101):
            api = make_local()
            install_schedule(api, schedule(charge=active_window(api)))
            api.client.state["soc_percent"] = soc
            await api.control_once()
            assert api.client.mode_calls == [], repr(soc)

    run_async(check())


def test_local_fresh_soc_takes_precedence_over_cached_soc():
    """A charge target reached since the previous poll cannot trigger another charge command."""

    async def check():
        api = make_local()
        api.state = copy.deepcopy(api.client.state)
        api._state_at = datetime.now(timezone.utc)
        api.client.state["soc_percent"] = 80
        install_schedule(api, schedule(charge=active_window(api, soc=80)))
        await api.control_once()
        assert api.client.mode_calls == [4]

    run_async(check())


def test_local_soc_crossing_charge_target_during_preflight_prevents_charge():
    """A target reached between the initial read and POST prevents the obsolete charge command."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api, soc=80)))
        api.client.preflight_state = dict(api.client.state, soc_percent=80)
        await api.control_once()
        assert 2 not in api.client.mode_calls
        await api.control_once()
        assert api.client.mode_calls[-1] == 4

    run_async(check())


def test_local_soc_crossing_reserve_during_preflight_prevents_export():
    """A battery reaching its floor while a command is prepared cannot receive forced export."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(export=active_window(api, soc=20), reserve=30))
        api.client.preflight_state = dict(api.client.state, soc_percent=30)
        await api.control_once()
        assert 3 not in api.client.mode_calls
        await api.control_once()
        assert api.client.mode_calls[-1] == 4

    run_async(check())


def test_local_read_only_during_preflight_prevents_charge():
    """A read-only transition during command preparation prevents the pending POST."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        api.client.before_mode = lambda: api.base.args.__setitem__("set_read_only", True)
        await api.control_once()
        assert 2 not in api.client.mode_calls
        assert 3 not in api.client.mode_calls

    run_async(check())


def test_local_low_power_enabled_during_preflight_prevents_forcing():
    """A newly enabled power limit invalidates a full-rate command before it reaches the gateway."""

    async def check():
        for flag, desired in (
            ("set_charge_low_power", schedule(charge=window())),
            ("set_export_low_power", schedule(export=window(soc=20))),
        ):
            api = make_local()
            api.get_minutes_now = lambda: 600
            install_schedule(api, desired)
            api.client.before_mode = lambda: api.base.args.__setitem__(flag, True)
            await api.control_once()
            assert 2 not in api.client.mode_calls
            assert 3 not in api.client.mode_calls
            await api.control_once()
            assert "wattage" in api.health_message().lower()

    run_async(check())


def test_local_new_revision_during_preflight_prevents_obsolete_charge():
    """A new committed schedule invalidates a previously prepared mode command."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        api.client.before_mode = lambda: install_schedule(api, schedule())
        await api.control_once()
        assert 2 not in api.client.mode_calls
        assert 3 not in api.client.mode_calls

    run_async(check())


def test_local_window_ending_during_preflight_prevents_charge():
    """A delayed preflight cannot start charging after the selected window has ended."""

    async def check():
        api = make_local()
        api.get_minutes_now = lambda: 600
        install_schedule(api, schedule(charge=window()))
        api.client.before_mode = lambda: setattr(api, "get_minutes_now", lambda: 660)
        await api.control_once()
        assert 2 not in api.client.mode_calls
        assert 3 not in api.client.mode_calls

    run_async(check())


def test_local_failed_read_releases_a_previously_owned_force():
    """Losing fresh SOC makes the controller relinquish a force instead of holding it indefinitely."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        api.client.read_error = RuntimeError("gateway unavailable")
        await api.control_once()
        assert api.client.mode_calls == [2, -1]

    run_async(check())


def test_local_failed_mode_write_does_not_confirm_the_plan():
    """A command failure keeps the intended revision pending and its health sensor off."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        api.client.mode_error = RuntimeError("command acknowledgement failed")
        await api.control_once()
        assert api._confirmed_revision != api._revision
        health = api.base.entities[f"binary_sensor.{api.prefix}_enphase_local_schedule_write_ok"]
        assert health["state"] == "off"

    run_async(check())


def test_local_low_power_settings_prevent_unsupported_force_commands():
    """Neither charge nor export can promise a wattage-limited command this endpoint cannot send."""

    async def check():
        for flag, desired in (
            ("set_charge_low_power", schedule(charge=window())),
            ("set_export_low_power", schedule(export=window(soc=20))),
        ):
            api = make_local()
            api.get_minutes_now = lambda: 600
            api.base.args[flag] = True
            install_schedule(api, desired)
            await api.control_once()
            assert not api.client.mode_calls
            assert "wattage" in api.health_message().lower()
            health = api.base.entities[f"binary_sensor.{api.prefix}_enphase_local_schedule_write_ok"]
            assert health["state"] == "off"
            assert "wattage" in health["attributes"]["error"].lower()

    run_async(check())


def test_local_enabling_low_power_releases_an_existing_force():
    """An incompatible planner setting relinquishes an active force and preserves its health reason."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        api.base.args["set_charge_low_power"] = True
        await api.control_once()
        assert api.client.mode_calls == [2, -1]
        assert not api._owned
        assert "wattage" in api.health_message().lower()

    run_async(check())


def test_local_requires_durable_ownership_before_forcing():
    """A failed Storage save prevents a command whose ownership would be lost on restart."""

    async def check():
        api = make_local()
        api.storage.fail_save = True
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        assert not api.client.mode_calls
        assert api._confirmed_revision != api._revision

    run_async(check())


def test_local_requires_storage_before_forcing():
    """A missing Storage component cannot leave an unrecorded gateway override behind."""

    async def check():
        api = make_local()
        api.base.components.components["storage"] = None
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        assert not api.client.mode_calls

    run_async(check())


def test_local_restart_recovers_and_releases_persisted_ownership():
    """A new process relinquishes a persisted force before accepting any new schedule."""

    async def check():
        original = make_local()
        install_schedule(original, schedule(charge=active_window(original)))
        await original.control_once()
        restored = make_local()
        restored.base.components.components["storage"] = original.storage
        restored.client.state = copy.deepcopy(original.client.state)
        await restored.control_once()
        assert restored.schedule is None
        assert restored.client.mode_calls == [-1]
        assert not restored._owned

    run_async(check())


def test_local_new_plan_is_pending_until_gateway_confirmation():
    """Committing a revision cannot publish it as applied before the worker verifies it."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        confirmed = api._confirmed_revision
        await api.switch_event(entity(api, "charge_enable"), "turn_off")
        await api.switch_event(entity(api, "charge_write"), "turn_on")
        assert api._revision > confirmed
        assert api._confirmed_revision == confirmed
        health = api.base.entities[f"binary_sensor.{api.prefix}_enphase_local_schedule_write_ok"]
        assert health["state"] == "off"

    run_async(check())


def test_local_callbacks_remain_responsive_while_a_read_is_pending():
    """Planner callbacks can supersede a waiting read without issuing the old charge intent."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        api.client.read_entered = asyncio.Event()
        api.client.read_gate = asyncio.Event()
        pending = asyncio.create_task(api.control_once())
        try:
            await asyncio.wait_for(api.client.read_entered.wait(), timeout=1)
            await asyncio.wait_for(api.switch_event(entity(api, "charge_enable"), "turn_off"), timeout=1)
            await asyncio.wait_for(api.switch_event(entity(api, "charge_write"), "turn_on"), timeout=1)
        finally:
            api.client.read_gate.set()
            await pending
        assert 2 not in api.client.mode_calls
        assert 3 not in api.client.mode_calls
        assert not api.schedule["charge"]["enable"]

    run_async(check())


def test_local_automatic_config_preserves_other_telemetry():
    """Local control binds its own controls and SOC without replacing established history sensors."""

    async def check():
        api = make_local()
        preserved = {
            "load_today": ["sensor.house_consumption_today"],
            "import_today": ["sensor.grid_import_today"],
            "export_today": ["sensor.grid_export_today"],
            "pv_today": ["sensor.roof_generation_today"],
            "load_power": ["sensor.house_power"],
            "pv_power": ["sensor.pv_power"],
            "grid_power": ["sensor.grid_power"],
            "battery_power": ["sensor.battery_power"],
        }
        api.base.args.update(copy.deepcopy(preserved))
        stale_controls = (
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
        )
        api.base.args.update({name: [f"number.previous_controller_{name}"] for name in stale_controls})
        api.state = copy.deepcopy(api.client.state)
        api._state_at = datetime.now(timezone.utc)
        await api.automatic_config()
        assert api.base.args["inverter_type"] == ["EnphaseLocal"]
        assert api.base.args["num_inverters"] == 1
        assert api.base.args["soc_percent"] == [f"sensor.{api.prefix}_enphase_local_soc_percent"]
        assert api.base.args["charge_start_time"] == [entity(api, "charge_start_time")]
        assert api.base.args["schedule_write_button"] == [entity(api, "charge_write")]
        assert api.base.args["scheduled_discharge_enable"] == [entity(api, "export_enable")]
        assert api.base.args["discharge_target_soc"] == [entity(api, "reserve")]
        assert {name: api.base.args[name] for name in preserved} == preserved
        assert not set(stale_controls).intersection(api.base.args)

    run_async(check())


def test_local_real_inverter_export_target_reaches_the_committed_schedule():
    """The actual planner service path delivers its export target without changing future charge SOC."""
    from config import INVERTER_DEF
    from inverter import Inverter

    api = make_local()
    api.get_minutes_now = lambda: 600
    api.state = copy.deepcopy(api.client.state)
    api._state_at = datetime.now(timezone.utc)
    install_schedule(api, schedule(charge=window("12:00:00", "14:00:00", soc=80)))
    run_async(api.automatic_config())
    api.publish_schedule_settings_ha()
    base = api.base
    base.last_service_hash = {}
    base.set_inverter_notify = False
    calls = []

    def resolve_arg(template, value, extra_args=None, **kwargs):
        """Resolve the target placeholder through the same callback used by service templates."""
        return value.format(**(extra_args or {})) if isinstance(value, str) else value

    def call_service(service, **data):
        """Dispatch actual service-template outputs to the component's HA event boundary."""
        calls.append((service, copy.deepcopy(data)))
        if service == "number/set_value":
            run_async(api.number_event(data["entity_id"], data["value"]))
        elif service in ("switch/turn_on", "switch/turn_off"):
            run_async(api.switch_event(data["entity_id"], service.split("/")[1]))
        else:
            raise AssertionError(f"Unexpected inverter service: {service}")
        return True

    base.resolve_arg = resolve_arg
    base.call_service_wrapper = call_service
    inverter = Inverter.__new__(Inverter)
    inverter.base = base
    inverter.id = 0
    inverter.log = base.log
    inverter.reserve_percent = 5
    inverter.inv_has_target_soc = INVERTER_DEF["EnphaseLocal"]["has_target_soc"]
    inverter.inv_target_soc_used_for_discharge = INVERTER_DEF["EnphaseLocal"]["target_soc_used_for_discharge"]
    inverter.inv_time_button_press = True
    inverter.inv_has_solis_energy_control = False
    inverter.registers_moved = 0
    inverter.soc_percent = 60
    inverter.get_current_discharge_rate = lambda: 3500
    inverter.mqtt_message = lambda *args, **kwargs: None

    def write_value(name, entity_id, value, **kwargs):
        """Route an actual generic target write through its number entity."""
        run_async(api.number_event(entity_id, value))
        inverter.registers_moved += 1
        return True

    def commit_button(**kwargs):
        """Commit the generic inverter's staged register update."""
        run_async(api.switch_event(entity(api, "charge_write"), "turn_on"))
        return True

    inverter.write_and_poll_value = write_value
    inverter.press_and_poll_button = commit_button

    # The window commit can precede the immediate service carrying its actual export target.
    # It must remain unarmed during that interval instead of discharging to a stale reserve.
    run_async(api.select_event(entity(api, "export_start_time"), "09:00:00"))
    run_async(api.select_event(entity(api, "export_end_time"), "11:00:00"))
    run_async(api.switch_event(entity(api, "export_enable"), "turn_on"))
    run_async(api.switch_event(entity(api, "export_write"), "turn_on"))
    run_async(api.control_once())
    assert 3 not in api.client.mode_calls

    # This is the real Inverter route used by execute.py, not a direct component number event.
    inverter.adjust_battery_target(50, isExporting=True)
    assert api.schedule["charge"]["soc"] == 80
    inverter.adjust_export_immediate(50)
    assert api.schedule["export"]["soc"] == 50
    assert api.schedule["charge"]["soc"] == 80
    assert [service for service, _ in calls] == ["switch/turn_on", "number/set_value", "switch/turn_on"]
    assert calls[0][1]["entity_id"] == entity(api, "export_enable")
    assert calls[-1][1]["entity_id"] == entity(api, "export_write")

    api.client.state["soc_percent"] = 49
    run_async(api.control_once())
    assert api.client.mode_calls[-1] != 3
    api.client.state["soc_percent"] = 60
    run_async(api.control_once())
    assert api.client.mode_calls[-1] == 3

    # A generic charge-target reset is independent of the committed export floor.
    inverter.adjust_battery_target(100)
    assert api.schedule["export"]["soc"] == 50
    run_async(api.control_once())
    assert api.client.mode_calls[-1] == 3

    inverter.adjust_export_immediate(100)
    assert not api.schedule["export"]["enable"]
    run_async(api.control_once())
    assert api.client.mode_calls[-1] == 1


def test_local_rejects_a_competing_cloud_controller():
    """An active cloud component must not race the local controller even when automatic is false."""

    async def check():
        api = make_local(automatic=False)
        api.base.components.components["enphase"] = object()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        assert not api.client.mode_calls
        assert api._confirmed_revision != api._revision

    run_async(check())


def test_local_final_releases_owned_mode_and_closes_transport():
    """Stopping the component cleans up its force without leaving a running transport."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        await api.stop()
        await api.final()
        assert api.client.mode_calls == [2, -1]
        assert api.client.closed
        assert api.client.close_calls == 1

    run_async(check())


def test_local_cleanup_retains_storage_after_component_registry_is_cleared():
    """Shutdown can persist a release even after the manager has removed its Storage entry."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        await api.control_once()
        retained_storage = api.storage
        api.base.components.components.clear()
        await api.stop()
        await api.final()
        assert api.client.mode_calls == [2, -1]
        assert not api._owned
        assert any(data.get("owned") is False for data in retained_storage.data.values())
        assert api.client.close_calls == 1

    run_async(check())


def test_local_cross_loop_stop_waits_for_owner_cleanup():
    """A replacement cannot start while the previous component is still releasing its override."""

    async def check():
        api = make_local()
        install_schedule(api, schedule(charge=active_window(api)))
        owner_loop = asyncio.new_event_loop()
        owner_thread = threading.Thread(target=owner_loop.run_forever, name="enphase-test-owner")
        owner_thread.start()
        stopping = None

        async def bootstrap():
            """Create loop-bound gates and start the real owner-loop worker."""
            api.client.release_gate = asyncio.Event()
            api.client.release_signal = threading.Event()
            return await api.run(seconds=0, first=True)

        try:
            started = asyncio.run_coroutine_threadsafe(bootstrap(), owner_loop)
            assert await asyncio.wait_for(asyncio.wrap_future(started), timeout=3)
            assert api.client.mode_calls == [2]
            stopping = asyncio.create_task(api.stop())
            assert await asyncio.to_thread(api.client.release_signal.wait, 3)
            assert not stopping.done(), "stop returned while its owned override was still pending release"
            assert not api.client.closed
            owner_loop.call_soon_threadsafe(api.client.release_gate.set)
            await asyncio.wait_for(stopping, timeout=3)
            assert api.client.closed
            assert not api._owned
            assert api.client.mode_calls == [2, -1]
            finalized = asyncio.run_coroutine_threadsafe(api.final(), owner_loop)
            await asyncio.wait_for(asyncio.wrap_future(finalized), timeout=3)
            assert api.client.mode_calls == [2, -1]
            assert api.client.close_calls == 1
        finally:
            if api.client.release_gate is not None:
                owner_loop.call_soon_threadsafe(api.client.release_gate.set)
            if stopping is not None and not stopping.done():
                try:
                    await asyncio.wait_for(stopping, timeout=3)
                except Exception:
                    pass
            cleanup = asyncio.run_coroutine_threadsafe(api.final(), owner_loop)
            try:
                await asyncio.wait_for(asyncio.wrap_future(cleanup), timeout=3)
            finally:
                owner_loop.call_soon_threadsafe(owner_loop.stop)
                owner_thread.join(timeout=3)
                assert not owner_thread.is_alive(), "test owner loop did not stop"
                owner_loop.close()

    run_async(check())


def test_local_blocked_ha_publication_does_not_block_battery_control():
    """Slow HA publishing cannot stall fresh control, callbacks or release and cannot revert the cache."""

    async def check():
        api = make_local()
        interface = BlockingLocalHA(api.base)
        api.base.ha_interface = interface
        api.base.dashboard_values = {}
        api.base.dashboard_index_app = {}
        install_schedule(api, schedule(charge=active_window(api)))
        owner_loop = asyncio.new_event_loop()
        owner_thread = threading.Thread(target=owner_loop.run_forever, name="enphase-test-ha-owner")
        owner_thread.start()
        try:
            started = asyncio.run_coroutine_threadsafe(api.run(seconds=0, first=True), owner_loop)
            assert await asyncio.wait_for(asyncio.wrap_future(started), timeout=3)
            assert await asyncio.to_thread(interface.entered.wait, 3)
            assert api.client.mode_calls == [2]

            async def change_targets():
                """Exercise actual callbacks repeatedly while one obsolete publication is blocked."""
                for target in range(60, 91):
                    await api.number_event(entity(api, "charge_soc"), target)

            staged = asyncio.run_coroutine_threadsafe(change_targets(), owner_loop)
            await asyncio.wait_for(asyncio.wrap_future(staged), timeout=3)
            target_entity = entity(api, "charge_soc")
            assert interface.state_data[target_entity]["state"] == 90
            with api._publish_condition:
                assert api._publish_pending[target_entity]["state"] == 90
                assert len(api._publish_pending) <= len(api._publish_latest)
            assert len(interface.calls) == 1

            api.client.state["soc_percent"] = 70
            refreshed = asyncio.run_coroutine_threadsafe(api.control_once(), owner_loop)
            await asyncio.wait_for(asyncio.wrap_future(refreshed), timeout=3)
            soc_entity = f"sensor.{api.prefix}_enphase_local_soc_percent"
            assert interface.state_data[soc_entity]["state"] == 70
            assert interface.state_data[target_entity]["state"] == 90

            await asyncio.wait_for(api.stop(), timeout=3)
            assert api.client.mode_calls == [2, -1]
            assert api.client.closed
            assert not interface.release.is_set()
            interface.release.set()
            await asyncio.to_thread(api._publisher.join, 3)
            assert not api._publisher.is_alive()
            assert len(interface.calls) == 1
            assert interface.calls[0][2] is True
            assert interface.state_data[soc_entity]["state"] == 70
            assert interface.state_data[target_entity]["state"] == 90
        finally:
            interface.release.set()
            cleanup = asyncio.run_coroutine_threadsafe(api.final(), owner_loop)
            try:
                await asyncio.wait_for(asyncio.wrap_future(cleanup), timeout=3)
            finally:
                owner_loop.call_soon_threadsafe(owner_loop.stop)
                owner_thread.join(timeout=3)
                assert not owner_thread.is_alive(), "test HA owner loop did not stop"
                owner_loop.close()
                if api._publisher is not None:
                    api._publisher.join(timeout=3)
                    assert not api._publisher.is_alive(), "test HA publisher did not stop"

    run_async(check())


def test_local_ha_error_json_keeps_publication_retryable():
    """A parsed HA error object cannot consume a snapshot as though its state POST succeeded."""

    async def check():
        api = make_local()
        api.poll_seconds = 0.01  # Only the independent publisher retry timer runs in this test.
        interface = BlockingLocalHA(api.base)
        api.base.ha_interface = interface
        retried = threading.Event()
        requests = []

        def api_call(endpoint, data_in=None, post=False, **kwargs):
            """Reject the first submission and acknowledge its retry with an actual entity response."""
            requests.append((endpoint, copy.deepcopy(data_in), post))
            if len(requests) == 1:
                return {"message": "bad request"}
            retried.set()
            return dict(data_in, entity_id=endpoint.split("/api/states/", 1)[1])

        interface.api_call = api_call
        try:
            api.dashboard_item("sensor.predbat_enphase_local_retry_probe", 50, {"friendly_name": "Retry Probe"}, app="enphase_local")
            assert await asyncio.to_thread(retried.wait, 3), "HA error JSON was treated as a successful publication"
            assert requests[0] == requests[1]
            assert requests[1][2] is True
        finally:
            await api.stop()
            if api._publisher is not None:
                api._publisher.join(timeout=3)
                assert not api._publisher.is_alive(), "test retry publisher did not stop"

    run_async(check())


def test_local_unchanged_entity_refreshes_after_ha_restart():
    """Unchanged local controls are republished periodically and immediately repair stale HA cache events."""

    async def check():
        api = make_local()
        interface = BlockingLocalHA(api.base)
        api.base.ha_interface = interface
        requests = []
        refreshed = threading.Event()
        entity_id = "sensor.predbat_enphase_local_refresh_probe"
        attributes = {"friendly_name": "Refresh Probe"}

        def api_call(endpoint, data_in=None, post=False, **kwargs):
            """Acknowledge each publication and notify the test of the periodic refresh."""
            requests.append((endpoint, copy.deepcopy(data_in), post))
            if len(requests) == 2:
                refreshed.set()
            return dict(data_in, entity_id=endpoint.split("/api/states/", 1)[1])

        interface.api_call = api_call
        try:
            api.dashboard_item(entity_id, 50, attributes, app="enphase_local")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with api._publish_condition:
                    delivered = entity_id in api._publish_delivered_at and api._publish_inflight is None
                if delivered:
                    break
                await asyncio.sleep(0.01)
            assert delivered, "initial HA entity publication did not finish"
            assert len(requests) == 1
            with api._publish_condition:
                api._publish_delivered_at[entity_id] = time.monotonic() - 61
            interface.state_data[entity_id]["state"] = "unknown"
            api.dashboard_item(entity_id, 50, attributes, app="enphase_local")
            assert interface.state_data[entity_id]["state"] == 50
            assert await asyncio.to_thread(refreshed.wait, 3), "unchanged entity was never restored after HA restart"
            assert requests[0] == requests[1]
        finally:
            await api.stop()
            if api._publisher is not None:
                api._publisher.join(timeout=3)
                assert not api._publisher.is_alive(), "test refresh publisher did not stop"

    run_async(check())


def test_local_final_preserves_an_override_it_did_not_own():
    """Cleanup does not release another application's mode when this component never controlled it."""

    async def check():
        api = make_local()
        api.client.state["override_mode"] = 3
        await api.final()
        assert api.client.mode_calls == []
        assert api.client.closed

    run_async(check())


def run_enphase_local_tests(my_predbat):
    """Execute every local-controller regression and let an assertion fail the registry group."""
    tests = [(name, function) for name, function in globals().items() if name.startswith("test_local_") and callable(function)]
    for name, function in sorted(tests):
        function()
        print(f"{name}: PASSED")
    print(f"Enphase local controller: {len(tests)} tests passed")
    return False
