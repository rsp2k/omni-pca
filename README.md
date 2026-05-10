# omni-pca

Async Python client for HAI/Leviton Omni-Link II home automation panels — Omni Pro II, Omni IIe, Omni LTe, Lumina.

Includes a Home Assistant custom component (`custom_components/omni_pca/`).

## Status

**Alpha.** Built from a full reverse-engineering of HAI's PC Access 3.17 (the Windows installer/programmer app). The protocol layer captures two non-public quirks that public Omni-Link clients miss:

1. **Session key is not the ControllerKey.** Last 5 bytes are XORed with a controller-supplied SessionID nonce.
2. **Per-block XOR pre-whitening before AES.** First two bytes of every 16-byte block are XORed with the packet's sequence number.

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the full byte-level spec.

## Quick start (library)

```bash
uv add omni-pca
```

```python
import asyncio
from omni_pca import OmniClient

async def main():
    async with OmniClient(
        host="192.168.1.9",
        port=4369,
        controller_key=bytes.fromhex("6ba7b4e9b4656de3cd7edd4c650cdb09"),
    ) as panel:
        info = await panel.get_system_info()
        print(info.model_name, info.firmware_version)

asyncio.run(main())
```

## Quick start (Home Assistant)

Copy `custom_components/omni_pca/` into your HA `config/custom_components/`, restart HA, then add the integration via Settings → Devices & Services. You'll need:

- Panel IP / hostname
- TCP port (default 4369)
- ControllerKey as 32 hex chars

Get the ControllerKey from your `.pca` file using the included parser:

```bash
uvx --from omni-pca omni-pca decode-pca path/to/Your.pca --field controller_key
```

The integration creates one HA device per panel plus typed entities for every named object on the controller: `alarm_control_panel` for areas, `light` for units, `binary_sensor`/`switch` for zones (state + bypass), `climate` for thermostats, `sensor` for analog zones and panel telemetry, `button` for panel macros, and `event` for the typed push-notification stream. See [`custom_components/omni_pca/README.md`](custom_components/omni_pca/README.md) for the entity table and service list.

## Without a panel — mock controller

For testing, the library ships a minimal Omni controller emulator:

```python
from omni_pca.mock_panel import MockPanel

async with MockPanel(controller_key=...).serve(port=14369):
    # connect a real OmniClient to localhost:14369 — works end-to-end
    ...
```

## Versioning

Date-based ([CalVer](https://calver.org/)): `YYYY.M.D`. Bumped on backwards-incompatible changes.

## Acknowledgments

This client is independent and not affiliated with Leviton or HAI. Protocol details derived from clean-room analysis of the publicly-distributed PC Access installer.
