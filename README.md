# omni-pca

Async Python client for HAI/Leviton Omni-Link II home automation panels — Omni Pro II, Omni IIe, Omni LTe, Lumina.

Includes a Home Assistant custom component (`custom_components/omni_pca/`).

**Project home:** <https://git.supported.systems/warehack.ing/omni-pca>
**Documentation:** <https://hai-omni-pro-ii.warehack.ing/>

## Status

**Alpha.** Built from a full reverse-engineering of HAI's PC Access 3.17 (the Windows installer/programmer app). The protocol layer captures two non-public quirks that public Omni-Link clients miss:

1. **Session key is not the ControllerKey.** Last 5 bytes are XORed with a controller-supplied SessionID nonce.
2. **Per-block XOR pre-whitening before AES.** First two bytes of every 16-byte block are XORed with the packet's sequence number.

The full byte-level protocol spec lives at <https://hai-omni-pro-ii.warehack.ing/reference/protocol/>.

## Install

The library isn't on PyPI yet (pending), so install directly from the Gitea release:

```bash
# Pinned to a specific release (recommended)
pip install "omni-pca @ git+https://git.supported.systems/warehack.ing/omni-pca.git@v2026.5.10"

# Or the wheel from the release page
pip install https://git.supported.systems/warehack.ing/omni-pca/releases/download/v2026.5.10/omni_pca-2026.5.10-py3-none-any.whl

# Or with uv
uv add "omni-pca @ git+https://git.supported.systems/warehack.ing/omni-pca.git@v2026.5.10"
```

Once published to PyPI, the canonical install will be `pip install omni-pca`.

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

## Quick start (Home Assistant)

```bash
# Manual install — works on every HA flavour
cd /path/to/your/homeassistant/config/
mkdir -p custom_components
cd custom_components
git clone https://git.supported.systems/warehack.ing/omni-pca tmp-omni
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
