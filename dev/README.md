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

## Notes

- The HA container mounts `../custom_components/omni_pca/` read-only, so
  edits to the integration need a restart (`docker compose restart
  homeassistant`) to take effect.
- The mock panel binds `0.0.0.0:14369` inside the container. If you
  prefer to talk to it from the host directly (e.g. with `omni-pca`
  CLI), use `make dev-mock` to run it natively.
