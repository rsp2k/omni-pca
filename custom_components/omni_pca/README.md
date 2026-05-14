# HAI / Leviton Omni Panel — Home Assistant Integration

Native HA integration that talks Omni-Link II directly to your **Omni Pro II
/ Omni IIe / Omni LTe / Lumina** controller over TCP. No middleware — HA
opens an encrypted session straight to the panel and listens for unsolicited
push messages.

This integration is the HA-facing wrapper around the
[`omni-pca`](https://github.com/rsp2k/omni-pca) Python library; the library
handles the wire protocol, this component surfaces it as HA entities.

## Install

### HACS

1. HACS → Integrations → search **HAI / Leviton Omni Panel**.
2. Install, then restart Home Assistant.

(If not yet in the HACS default catalog: HACS → Integrations → custom
repository → add `https://github.com/rsp2k/omni-pca`, category **Integration**.)

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
3. Save. The panel appears as a single device with entities per object.

### Where do I get the Controller Key?

If you have a `.pca` configuration export from PC Access, the included CLI
extracts the key for you:

```bash
uvx omni-pca decode-pca '/path/to/My House.pca' --field controller_key
```

Otherwise, find it in PC Access under the panel's **Setup → Misc → Network**
page (HAI labels it "Encryption Key 1").

## Entities created

One device per panel, plus per-object entities below.

| Platform | Entity | Per |
|---|---|---|
| `alarm_control_panel` | Area arm/disarm with code | discovered area |
| `binary_sensor` | Zone open/tripped | binary zone |
| `binary_sensor` | Zone bypassed (diagnostic) | binary zone |
| `binary_sensor` | AC power, backup battery, system trouble | panel |
| `button` | Panel button macro | discovered button |
| `climate` | Thermostat (heat/cool/auto, fan, hold) | discovered thermostat |
| `event` | Typed push event relay | panel |
| `light` | Unit on/off + brightness | discovered unit |
| `sensor` | Analog zone (temp/humidity/power) | analog zone |
| `sensor` | Thermostat current temp / humidity / outdoor temp | thermostat |
| `sensor` | Panel model + firmware, last event class | panel |
| `switch` | Zone bypass toggle | binary zone |

State propagates via the panel's unsolicited push messages: zone changes,
arming changes, AC/battery troubles, etc. all arrive within one TCP round-
trip. A 30-second background poll backstops anything that didn't push.

## Services

| Service | Purpose |
|---|---|
| `omni_pca.bypass_zone` | Bypass a zone by 1-based index |
| `omni_pca.restore_zone` | Restore a previously-bypassed zone |
| `omni_pca.execute_program` | Run a stored program by index |
| `omni_pca.show_message` | Display a stored message on consoles |
| `omni_pca.clear_message` | Clear a displayed message |
| `omni_pca.acknowledge_alerts` | Clear all outstanding troubles/alerts |
| `omni_pca.send_command` | Power-user escape hatch (raw Command opcode) |

Every service takes an `entry_id` so it picks the right panel when you have
multiple configured.

## Automation example

React to any alarm activation in real time:

```yaml
automation:
  - alias: Notify on alarm
    trigger:
      - platform: event
        event_type: state_changed
        event_data:
          entity_id: event.panel_events
    condition: >
      {{ trigger.event.data.new_state.attributes.event_type ==
         "alarm_activated" }}
    action:
      - service: notify.mobile_app
        data:
          title: ALARM
          message: >
            Area {{ trigger.event.data.new_state.attributes.area_index }}
```

## Diagnostics

Settings → Devices & Services → *HAI/Leviton Omni Panel* → ⋮ → **Download
diagnostics** dumps a redacted snapshot (controller key removed, zone names
hashed) — useful for bug reports.

## Troubleshooting

- **Won't connect**: confirm port 4369 is open on the panel. The Omni Pro
  II's network module ships *off* by default; enable it under Setup → Misc
  → Network on a console.
- **Authentication failed**: re-check the Controller Key. The integration
  triggers HA's reauth flow when the panel rejects the key.
- **No entities for X**: only objects with a name configured on the panel
  are discovered. PC Access's "Names" page is where they live.

See the [parent README](https://github.com/rsp2k/omni-pca) for protocol /
library details. Detailed reverse-engineering notes are in
[`docs/JOURNEY.md`](https://github.com/rsp2k/omni-pca/blob/main/docs/JOURNEY.md).
