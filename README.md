# omni-pca

Async Python client for HAI/Leviton Omni-Link II home automation panels — Omni Pro II, Omni IIe, Omni LTe, Lumina.

Includes a Home Assistant custom component (`custom_components/omni_pca/`).

**Project home:** <https://github.com/rsp2k/omni-pca>
**Documentation:** <https://hai-omni-pro-ii.warehack.ing/>

## Status

**Alpha.** Built from a full reverse-engineering of HAI's PC Access 3.17 (the Windows installer/programmer app). The protocol layer captures two non-public quirks that public Omni-Link clients miss:

1. **Session key is not the ControllerKey.** Last 5 bytes are XORed with a controller-supplied SessionID nonce.
2. **Per-block XOR pre-whitening before AES.** First two bytes of every 16-byte block are XORed with the packet's sequence number.

The full byte-level protocol spec lives at <https://hai-omni-pro-ii.warehack.ing/reference/protocol/>.

## Install

```bash
pip install omni-pca

# Or with uv
uv add omni-pca
```

For Home Assistant users, install the integration through HACS — see the [HA install how-to](https://hai-omni-pro-ii.warehack.ing/how-to/install-in-home-assistant/).

## Quick start (library)

```python
import asyncio
from omni_pca import OmniClient

async def main():
    async with OmniClient(
        host="192.168.1.9",
        port=4369,
        controller_key=bytes.fromhex("6ba7b4e9b4656de3cd7edd4c650cdb09"),
    ) as panel:
        info = await panel.get_system_information()
        print(info.model_name, info.firmware_version)

asyncio.run(main())
```

For the panel walkthrough — connect, list zones, react to push events — see the [tutorial](https://hai-omni-pro-ii.warehack.ing/tutorials/first-script/).

## Two wire dialects — TCP/v2 vs UDP/v1

The Omni network module is configurable at the panel keypad to listen on **TCP, UDP, or both**. Each transport speaks a different wire dialect — `OmniClient` above handles the TCP path (OmniLink2, the modern wire format used by PC Access ≥ 3); panels configured UDP-only fall back to the legacy v1 protocol with typed `RequestZoneStatus` / `RequestUnitStatus` opcodes, no `RequestProperties`, and streaming name downloads. For those, use [`OmniClientV1`](https://hai-omni-pro-ii.warehack.ing/reference/library-api/#v1-udp-omniclientv1) from the `omni_pca.v1` subpackage:

```python
from omni_pca.v1 import OmniClientV1

async with OmniClientV1(
    host="192.168.1.9",
    controller_key=bytes.fromhex("..."),
) as panel:
    info = await panel.get_system_information()      # same dataclass as v2
    names = await panel.list_all_names()             # streaming UploadNames
    zones = await panel.get_zone_status(1, 16)       # typed status by range
    await panel.execute_security_command(area=1, mode=SecurityMode.AWAY, code=1234)
```

The HA integration picks the right client automatically based on the **Transport** dropdown in the config flow (TCP vs UDP). See [zone & unit numbering](https://hai-omni-pro-ii.warehack.ing/explanation/zone-unit-numbering/) for why v1 panels need the long-form `RequestUnitStatus` for unit indices > 255.

## Quick start (Home Assistant)

```bash
# Manual install — works on every HA flavour
cd /path/to/your/homeassistant/config/
mkdir -p custom_components
cd custom_components
git clone https://github.com/rsp2k/omni-pca tmp-omni
cp -r tmp-omni/custom_components/omni_pca .
rm -rf tmp-omni
```

Restart HA, then add the integration via **Settings → Devices & Services**. You'll need:

- Panel IP / hostname
- TCP port (default 4369)
- ControllerKey as 32 hex chars

Get the ControllerKey from your `.pca` file using the bundled CLI:

```bash
omni-pca decode-pca '/path/to/Your.pca' --field controller_key
```

The integration creates one HA device per panel plus typed entities for every named object on the controller: `alarm_control_panel` for areas, `light` for units, `binary_sensor` + `switch` for zones (state + bypass), `climate` for thermostats, `sensor` for analog zones and panel telemetry, `button` for panel macros, and `event` for the typed push-notification stream. See [`custom_components/omni_pca/README.md`](custom_components/omni_pca/README.md) for the full entity + service catalog, or the [HA install how-to](https://hai-omni-pro-ii.warehack.ing/how-to/install-in-home-assistant/) for the step-by-step.

## Without a panel — mock controller

The library ships a stateful `MockPanel` that emulates the controller side of the protocol over real TCP. Useful for offline development, integration tests, and demos:

```python
from omni_pca.mock_panel import MockPanel

async with MockPanel(controller_key=...).serve(port=14369):
    # Connect a real OmniClient to localhost:14369 — full handshake + AES
    ...
```

The local dev stack (`dev/docker-compose.yml`) packages a real Home Assistant container and the mock panel side-by-side so you can click through the integration without touching real hardware. See [the dev-stack tutorial](https://hai-omni-pro-ii.warehack.ing/tutorials/dev-stack/).

## Tests

```bash
uv sync --group ha
uv run pytest -q
```

351 tests across the protocol primitives, the mock panel, the OmniClient ↔ MockPanel end-to-end roundtrip, and an in-process Home Assistant harness driving the integration via the real config flow + service calls.

## Versioning

Date-based ([CalVer](https://calver.org/)): `YYYY.M.D`. Bumped on backwards-incompatible changes. See [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT. See [`LICENSE`](LICENSE).

## Acknowledgments

This client is independent and not affiliated with Leviton or HAI. Protocol details derived from clean-room analysis of the publicly-distributed PC Access installer. The reverse-engineering arc is documented at <https://hai-omni-pro-ii.warehack.ing/journey/>.
