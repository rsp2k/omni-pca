# Dev stack

Local Home Assistant + MockPanel for clicking around the integration without a
real Omni controller. Useful for screenshots, manual smoke tests, and seeing
what the entity layout looks like.

## Quick start

```bash
cd dev/
make dev-up         # docker compose up -d
# wait ~30s for HA to boot
open http://localhost:8123
```

First time: HA onboarding wizard (any name / location works). Then:

1. **Settings → Devices & Services → Add Integration**
2. Search for **HAI/Leviton Omni Panel**
3. Fill in:
   - host: `host.docker.internal`
   - port: `14369`
   - controller key: `000102030405060708090a0b0c0d0e0f`
4. Submit. Within a few seconds you should see the Omni Pro II device with
   ~25 entities (binary sensors, lights, alarm panel, climate, sensors,
   buttons, switches, the events entity).

## What the mock simulates

Five named zones, four units, two areas, two thermostats, three button
macros. User codes `1234` (master, code index 1) and `5678` (code index 2).

Arming the alarm with code `1234` will succeed and the
`alarm_control_panel` entity transitions through ARMING → ARMED_AWAY in
real time via the panel's push-event simulation. Wrong code → HA error
toast, panel stays disarmed.

## Other targets

```bash
make dev-logs       # tail HA + mock logs
make dev-mock       # run only the mock on the host (no docker)
make dev-down       # stop the stack
make dev-reset      # wipe HA config and start fresh
```

## Load real `.pca` data into the mock

By default the mock serves a small synthetic state (five zones, four
units, …). Point `OMNI_PCA_FIXTURE` at a real `.pca` file to make the
mock indistinguishable on the wire from the source panel:

```bash
# dev/.env (gitignored)
OMNI_PCA_FIXTURE=/fixtures/path/to/Account.pca
```

The host directory `/home/kdm/home-auto/HAI` is mounted at `/fixtures`
inside the mock-panel container (see `docker-compose.yml`); adjust the
mount if your `.pca` lives elsewhere.

The decryption key is auto-derived from a sibling `PCA01.CFG` if one
exists (this is how PC Access exports usually ship). To override:

```bash
OMNI_PCA_FIXTURE_KEY=0xC1A280B2   # or --pca-key on the command line
```

`MockState.from_pca` populates zones, units, areas, thermostats,
buttons, programs, model byte, and firmware version from the file —
everything the HA integration reads at discovery time.

## Time-series & dashboards

`docker compose up -d` also brings up **InfluxDB v2** (port 8086) and
**Grafana** (port 3000). Open Grafana at <http://localhost:3000>
(login: `admin` / `$GRAFANA_PASSWORD` from `.env`) — the **Omni Pro II
— Panel Overview** dashboard loads automatically, pre-provisioned from
[`../grafana/`](../grafana/), the shipping bundle.

To wire HA → InfluxDB, append this block to `ha-config/configuration.yaml`
(the directory is gitignored because it contains HA auth/state; the
block lives in `../grafana/ha-snippet.yaml` for production users):

```yaml
influxdb:
  api_version: 2
  host: influxdb
  port: 8086
  ssl: false
  verify_ssl: false
  token: dev-token-omnipca-9472-fixed-for-dev-stack
  organization: omni-pca
  bucket: ha
  precision: s
  tags_attributes: [event_type, event_class]
  include:
    domains: [alarm_control_panel, binary_sensor, climate, event, light, sensor, switch]
    entity_globs: ["*omni*"]
```

Restart HA (`docker compose restart homeassistant`) after editing.
Within 30 seconds, panels start populating with live data.

The dashboard JSON in `../grafana/provisioning/dashboards/` is the
source of truth; edits in the Grafana UI don't persist (provisioned
dashboards are read-only). Iterate by editing the JSON and running
`docker compose restart grafana` — the provisioner picks up changes
within ~30s.

To exercise dashboard panels against the mock, trigger HA actions
(arm an area, toggle a light): the mock pushes the resulting
`SystemEvent` back to HA, which ships it to InfluxDB, which Grafana
queries. Each step takes <1s.

## Notes

- The HA container mounts `../custom_components/omni_pca/` read-only, so
  edits to the integration need a restart (`docker compose restart
  homeassistant`) to take effect.
- The mock panel binds `0.0.0.0:14369` inside the container. If you
  prefer to talk to it from the host directly (e.g. with `omni-pca`
  CLI), use `make dev-mock` to run it natively.
