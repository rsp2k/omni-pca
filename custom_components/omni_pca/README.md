# HAI / Leviton Omni Panel — Home Assistant Integration

Native HA integration that talks Omni-Link II directly to your **Omni Pro II
/ Omni IIe / Omni LTe / Lumina** controller over TCP. No middleware — HA
opens an encrypted session straight to the panel and listens for unsolicited
push messages.

This integration is the HA-facing wrapper around the
[`omni-pca`](https://github.com/rsp2k/omni-pca) Python library; the library
handles the wire protocol, this component surfaces it as HA entities.

## Install

### HACS (recommended once published)

1. HACS → Integrations → custom repository → add
   `https://github.com/rsp2k/omni-pca`, category **Integration**.
2. Install **HAI / Leviton Omni Panel**, then restart Home Assistant.

### Manual

Copy the `custom_components/omni_pca/` directory into your HA
`config/custom_components/` directory and restart HA.

## Configure

1. **Settings → Devices & Services → Add Integration** → search for
   *HAI/Leviton Omni Panel*.
2. Enter:
   - **Host** — IP or hostname of the panel (e.g. `192.168.1.50`)
   - **Port** — defaults to `4369` (HAI's reserved port)
   - **Controller Key** — 32 hex characters, the panel's NVRAM key
3. Save. The panel's model and firmware appear as a single device, with one
   `binary_sensor` per defined zone.

### Where do I get the Controller Key?

If you have a `.pca` configuration export from PC Access, the included CLI
extracts the key for you:

```bash
uvx omni-pca decode-pca '/path/to/My House.pca' --field controller_key
```

Otherwise, find it in PC Access under the panel's **Setup → Misc → Network**
page (HAI labels it "Encryption Key 1").

## What you get

- One **device** per panel — model + firmware reported in the UI.
- One **`binary_sensor`** per defined zone, named from the panel's own
  zone-name field. `OPENING` device class for door/window contacts,
  `MOTION` for interior PIRs, `SMOKE` for fire zones, etc., chosen by zone
  type when the panel reports one.
- **Push updates**: zone state changes propagate within a single round-trip
  thanks to unsolicited-message subscription. The 30-second poll is just a
  safety net.

## Roadmap

- Areas → `alarm_control_panel` entities
- Units → `light` / `switch` entities
- Thermostats → `climate`
- Aux sensors → `sensor`

See the [parent README](https://github.com/rsp2k/omni-pca) for protocol /
library details.
