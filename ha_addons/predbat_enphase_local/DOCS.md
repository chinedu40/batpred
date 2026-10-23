# Predbat Enphase Local - Testing

This add-on runs the custom Enphase Local version of Predbat on the new Home
Assistant at `192.168.86.57`. It has its own configuration directory and starts
with the entity prefix `predbat_enphase_test`, **Monitor** mode and **Read Only**
enabled. Installing it does not change the production instance at `192.168.86.38`.

This installation requires Home Assistant OS or another installation with a
working Supervisor add-on store. The add-on supports `amd64` and `aarch64`.
Home Assistant builds it locally from
the source commit pinned in `source.json`; the first installation can take several
minutes. It runs as a non-root user and uses Home Assistant ingress for its web
interface. It does not publish a host port or an MCP endpoint.

## Install on the new Home Assistant

1. Open Home Assistant at <http://192.168.86.57:8123>, or your usual HTTPS address
   for that instance.
2. Go to **Settings → Apps → Install app**, or **Settings → Add-ons → Add-on
   Store**, depending on your Home Assistant version. Open the store's menu and
   choose **Repositories**.
3. Add this complete URL, including the branch fragment:

   ```text
   https://github.com/chinedu40/batpred#codex/enphase-local-api
   ```

4. Refresh the store and select **Predbat Enphase Local - Testing**. Choose
   **Install** and wait for the local image build to finish.
5. Start the add-on and check **Logs**. The first start creates a configuration
   template; it is not ready to control the battery until you configure it.
6. Edit the new add-on's `apps.yaml` and `secrets.yaml` as described below, restart
   it, and use **Open Web UI** to access Predbat through Home Assistant ingress.

Supervisor provides the connection to this Home Assistant automatically. You do
not need to copy an old Home Assistant URL, API key or Supervisor token.

## Find and configure its files

Use a file editor or SMB share on `192.168.86.57` that exposes add-on configuration.
The directory name ends in `_predbat_enphase_local`, normally under
`addon_configs`; some installations label the share `app_configs`. Edit the
`apps.yaml` and `secrets.yaml` in that directory. This is separate from the
production Predbat directory and from any other Predbat add-on.

Keep these settings in the generated `pred_bat` section of `apps.yaml`:

```yaml
prefix: predbat_enphase_test
mode: Monitor
set_read_only: true
auto_update: false
set_charge_low_power: false
set_export_low_power: false
enphase_local_automatic: true
```

Obtain a gateway JWT through Enphase's token portal. The component uses a token
you provide; it does not obtain or refresh it through an Enphase account. Put real
gateway values in `secrets.yaml`, for example:

```yaml
enphase_local_host: '192.168.86.20'  # Replace with your gateway address.
enphase_local_token: 'replace_with_gateway_JWT'
# Optional: certificate fingerprint verified through a trusted route.
# enphase_local_fingerprint: 'replace_with_trusted_SHA256_fingerprint'
```

Reference those entries inside the `pred_bat` section of `apps.yaml`:

```yaml
enphase_local_host: !secret enphase_local_host
enphase_local_token: !secret enphase_local_token
# enphase_local_fingerprint: !secret enphase_local_fingerprint
```

The host accepts a hostname/IP or an HTTPS authority URL, optionally with a port;
do not include a path or credentials. HTTPS verification stays enabled. Use a
certificate trusted by the runtime, or supply a trusted SHA-256 certificate
fingerprint. Do not accept an unverified fingerprint just to clear a TLS error.
Replace expired JWTs and restart the add-on.

Set `enphase_local_battery_rate_max` to your battery system's actual maximum power
in **watts**. The template's `10000` is an example, not a detected rating. A
positive value is required for automatic configuration. The gateway mode endpoint
cannot request an exact wattage, so keep both low-power options off. Capacity
normally comes from the gateway's `max_energy` value, converted from Wh to kWh;
set `enphase_local_capacity_kwh` only if a positive override is needed.

Replace every telemetry placeholder with an entity from the **new** Home
Assistant. The local component supplies SOC and mode, not household/PV/grid power
or daily energy counters. Keep your Home Assistant Enphase integration or meters
as telemetry sources. Daily energy bindings use kWh, power bindings use W; grid
power is positive for export and battery power is positive for discharge.
Configure your actual tariffs, rate sources, solar forecasts and timezone too.

Leave `template: true` while preparing the configuration. Remove it only after
replacing the placeholders. Restart, then verify that
`select.predbat_enphase_test_mode` is **Monitor** and
`switch.predbat_enphase_test_set_read_only` is **on**. Check the UI values as well
as the YAML because saved settings can override defaults.

## Copy selected production settings

On macOS, use Finder **Go → Connect to Server** with `smb://192.168.86.38` and
username `chinedu40`. Read the existing production configuration without changing
it. Connect separately to `smb://192.168.86.57` to edit the testing configuration;
back up destination files before replacing any settings.

Copy only the relevant energy/power bindings, tariff/rate settings, forecast
sources, timezone and battery model values. Check each entity in the new Home
Assistant: the same device can have a different entity ID there. Copy only the
individual rate/forecast secrets those settings need.

Do not copy the entire production directory, `ha_url`, `ha_key`, Supervisor
tokens, Enphase cloud credentials, runtime JSON, storage/ownership records,
databases, cached plans or saved control settings. Keep the new add-on's
Supervisor connection, distinct prefix, **Monitor** mode and **Read Only** setting.
Remove `enphase_username` and `enphase_password` from its configuration;
`enphase_automatic: false` does not disable cloud schedule writes.

Copying configuration leaves the production controller running. Keep the testing
instance in Monitor and Read Only while production controls the same physical
battery. A later handover needs an explicit decision to stop production control
and any other battery-control automation before enabling this instance.

## Verify before a later control handover

Check `sensor.predbat_enphase_test_enphase_local_soc_percent`, reported capacity,
gateway operating/override modes, telemetry units and the planned windows. Mode
sensors keep the numeric gateway value and include a `mode_name` attribute.
`binary_sensor.predbat_enphase_test_enphase_local_schedule_write_ok` confirms a
requested mode only after gateway readback; inspect its pending/error attributes
when diagnosing a request. It need not be on while merely monitoring.

The component uses gateway modes `2` (charge), `3` (forced discharge), `1`
(ZeroNet/self-consumption), `4` (NoDischarge) and `-1` (release an owned override).
**Control charge** permits charging; **Control charge & discharge** also permits
forced export. Monitor and Control SOC only do not issue force commands. Turn
Read Only off only as part of the later handover after stopping other controllers.

Control has been tested with simulated responses, not with live hardware from
this development environment. In particular, firmware support for forced
discharge mode `3` remains unvalidated. Software SOC targets/reserve have polling
and request latency. The gateway's backup-reserve setting is not changed; firmware
determines how its native limits behave during an override.

Normal shutdown attempts to release an override owned by this component, and
restart releases a previously recorded ownership claim before accepting a fresh
plan. No hardware expiry/TTL has been verified: a crash, power loss or LAN outage
can leave an override active. Check the gateway after an interrupted shutdown.

## Updates

Update through this add-on's releases in the Home Assistant store. Its image uses
an immutable full source commit from `source.json`; app files are read-only to
the running user. Predbat's in-app automatic update is disabled. Do not use the
Predbat update selector/card to install an official release over this testing
build, or assume `predbat_repository` selects a slash-containing feature branch.

An upstream sync alone does not change an installed add-on. The maintainer must
advance the pinned source when application files change, publish a new add-on
version, and you must install that update through Home Assistant. Review the
changelog, back up this add-on's configuration and check Monitor/Read Only again
after an update. Updating or reinstalling this testing add-on does not hand over
control from production.
