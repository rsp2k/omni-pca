# Grafana dashboard for omni_pca

InfluxDB v2 + Grafana stack pre-provisioned to visualise an HAI/Leviton
Omni Pro II panel via the `omni_pca` Home Assistant integration.
Drop-in for any existing HA install — no integration changes required.

![Dashboard overview](../dev/artifacts/screenshots/2026-05-17/grafana-dashboard-final.png)

## What you get

One dashboard, four rows:

- **System health** — AC power, backup battery, system trouble, event count (24h).
- **Security** — area arming state timeline, recent push-event log, zone trip timeline.
- **Climate** — per-thermostat current temperatures + setpoints, HVAC mode timeline.
- **Activity** — event rate by typed event class, unit brightness heatmap.

Data flows: HA entity state → HA's `influxdb:` integration → InfluxDB
v2 bucket → Grafana Flux queries → dashboard panels.

## Quick start (~5 minutes)

```bash
cd grafana/
cp .env.example .env
# Edit .env — set strong INFLUX_PASSWORD, INFLUX_TOKEN, GRAFANA_PASSWORD.
# Generate the token with: openssl rand -hex 32

docker compose up -d
```

Wait ~30 seconds. InfluxDB does first-boot setup (creates the
`omni-pca` org, `ha` bucket, admin token); Grafana then auto-provisions
the InfluxDB datasource and the dashboard.

Then add the influxdb integration to your Home Assistant config:

```bash
# Paste the contents of ha-snippet.yaml into your configuration.yaml.
# Add `influxdb_token: <your INFLUX_TOKEN from .env>` to your secrets.yaml.
# Restart HA.
```

Within ~30 seconds you should see real-time data populating the
dashboard at <http://localhost:3000> (login: `admin` / your
`GRAFANA_PASSWORD`).

## Networking notes

The default `ha-snippet.yaml` assumes HA and InfluxDB sit on the same
docker network and HA can reach `influxdb:8086` by container name.
Three common variants:

| HA layout | `host:` value |
|---|---|
| Same compose stack as this bundle | `influxdb` |
| HA on the host, InfluxDB in docker | `host.docker.internal` or your LAN IP |
| Different machine entirely | the InfluxDB host's IP / FQDN |

If you put either service behind a reverse proxy with TLS, set `ssl:
true` in the HA snippet and supply the public hostname.

## Iterating on the dashboard

The dashboard JSON at `provisioning/dashboards/omni-pro-ii.json` is
loaded read-only by the provisioner. To change it:

1. Edit the JSON directly, then `docker compose restart grafana`
   (provisioner picks up changes within ~30s).
2. Or use the Grafana UI to experiment, then **Dashboard settings →
   JSON Model → Save to file** and overwrite the file in this repo.

Provisioned dashboards can't be saved from the UI by design — this is
intentional, so the file on disk stays the source of truth.

## Extending coverage

The bundle is scoped to the `omni_pca` entity surface via the
`entity_globs: ["*omni*"]` filter in `ha-snippet.yaml`. Drop that
filter (or add a second `include:` block) if you want to graph other
HA entities alongside omni data — Grafana's datasource is general
InfluxDB v2, nothing in the dashboard JSON hard-codes omni-specific
field names beyond what you'd want to scope to anyway.

A few panel ideas not yet shipped:

- Alarm activation drill-down — filter the event log to
  `event_type == "alarm_activated"` and show the `alarm_type`
  (Burglary / Fire / Auxiliary / …) distribution.
- Zone trip rate histogram — `binary_sensor` zone changes per zone
  per hour, useful for spotting flaky sensors.
- Comm health — track integration coordinator state via the panel
  device's "Comm error" attribute.

## Files in this bundle

| File | Purpose |
|---|---|
| `docker-compose.yml` | InfluxDB v2 + Grafana services |
| `.env.example` | Required environment template |
| `ha-snippet.yaml` | HA configuration.yaml additions |
| `provisioning/datasources/influxdb.yml` | Auto-wires the datasource |
| `provisioning/dashboards/dashboards.yml` | Provisioner config |
| `provisioning/dashboards/omni-pro-ii.json` | The dashboard JSON |

## Troubleshooting

**"No data" in panels.** Most panels need either continuous state
updates (climate, security) or push events (event-driven panels).
Verify HA is shipping data:

```bash
docker exec -it omni-pca-influxdb influx query \
  'from(bucket:"ha") |> range(start:-5m) |> limit(n:5)' \
  --token "$INFLUX_TOKEN" --org omni-pca
```

If this returns rows, the pipeline is healthy and panels will fill in
as the panel does interesting things. If it's empty, check HA logs for
`[homeassistant.components.influxdb]` errors.

**Dashboard didn't auto-load.** Check `docker logs omni-pca-grafana
2>&1 | grep -i provision` — provisioner errors show up there.

**Stat panels show duplicate values.** Your HA has multiple entities
matching the regex (e.g. `omni_pro_ii_ac_power` AND
`omni_pro_ii_ac_power_2` from prior integration reloads). Clean up the
duplicates in HA's entity registry, or tighten the filter in the
dashboard JSON.
