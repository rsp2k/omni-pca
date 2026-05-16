# Changelog

All notable changes to this project. Date-based versioning ([CalVer](https://calver.org/), `YYYY.M.D`); each release date corresponds to a backwards-incompatible boundary.

## [2026.5.16] — 2026-05-16

Program viewer side panel + writeback API + docs link fix.

### Home Assistant integration

- Lit/TypeScript side panel for the program viewer (Phase C): filterable list, slide-in detail panel, structured-English token rendering, REF-token click-to-filter, live-state badges (SECURE / NOT READY / ON 60% / Away / 72°F) sourced from the coordinator, "Fire now" button calling `omni_pca/programs/fire` over the websocket.
- Program writeback: `DownloadProgram` wire path, HA write API, Clear / Clone UI in the side panel.
- esbuild bundle committed at `custom_components/omni_pca/www/panel.js` (~34 KB minified) so end-users don't need Node.
- `manifest.json`: `documentation` URL points at <https://hai-omni-pro-ii.warehack.ing/> (was the GitHub repo); matches the canonical docs site already referenced from `pyproject.toml`.

## [2026.5.14] — 2026-05-14

HACS publishing release — brand assets and validation tooling.

### Home Assistant integration

- `brand/icon.png` (256×256) + `brand/icon@2x.png` (512×512) shipped inline at `custom_components/omni_pca/brand/` for the HA 2026.3 brands-proxy API.
- WebSocket commands + side-panel registration for an in-HA custom panel surfacing decoded programs.
- `program_renderer`: structured-English token streams for the HA UI to render conditional logic.
- `program_engine`: real AND/OR condition evaluator (StateEvaluator decodes records against MockState; replaces the always-passes-AND/always-fails-OR stub).
- `program_engine`: EVENT programs + event taxonomy (Phase 4), clausal chains WHEN/AT/EVERY + AND/OR/THEN (Phase 5).
- `__init__.py`: `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)` to satisfy hassfest.
- `manifest.json`: keys sorted (domain, name, then alphabetical), HTML/markdown removed from i18n strings.
- Canonical URLs switched to `github.com/rsp2k/omni-pca` (was Gitea-only).

### Library

- `pca_file.py`: progressive `SetupData` decoding — zone types, area assignments, entry/exit delays, temperature format, code PINs, installer/PCAccess codes, perimeter chime, audible exit delay, DST, house code format, time clocks, latitude/longitude/timezone, account remarks extended, 9 per-family description tables, zone options, thermostat type + areas, time_adj / alarm_reset_time / arming_confirmation / two_way_audio scalars.
- `iter_programs()` for both v1 (UDP) and v2 (TCP) wire dialects.
- `mock_panel`: v1 `UploadPrograms` streaming + program-echo tests; `MockState.from_pca()` builds state from a real `.pca`.
- `programs`: multi-record decoder properties (firmware ≥3.0 records), structured-OP AND decoder properties, AND-record u16 fields documented as big-endian on disk.

### CI / packaging

- `.github/workflows/validate.yml`: HACS action + hassfest on push / PR / weekly.
- `pyproject.toml`: full `[project.urls]` with Repository / Issues / Changelog / Documentation.

## [2026.5.10] — 2026-05-10

First release. Working library + Home Assistant custom component, validated end-to-end against an in-process mock panel and a real HA instance running in Docker. Not yet validated against a live panel because the user's panel's network module is currently off.

### Protocol layer (the reverse engineering)

- Decompiled HAI's PC Access 3.17 (.NET) with ilspycmd; identified two namespaces — `HAI_Shared` (protocol/crypto/domain) and `PCAccess3` (UI). Decompilation lives in `pca-re/decompiled/`.
- Reverse-engineered the `.pca` and `PCA01.CFG` file format — Borland-Pascal LCG keystream XORed byte-by-byte. Two hardcoded keys:
  - `KEY_PC01 = 0x14326573` for `PCA01.CFG`
  - `KEY_EXPORT = 0x17569237` for import/export `.pca`
  Per-installation `.pca` files use a third key derived from the panel's installer code; that key is stored in plaintext inside `PCA01.CFG` after first-stage decryption.
- Documented the Omni-Link II wire protocol byte-for-byte (`pca-re/notes/handshake.md`), including **two non-public quirks** absent from `jomnilinkII`, `pyomnilink`, and every public Omni-Link writeup we found:
  1. **Session key = `ControllerKey[0:11] || (ControllerKey[11:16] XOR SessionID[0:5])`** — not just the panel's ControllerKey directly. Source: `clsOmniLinkConnection.cs:1886-1892`.
  2. **Per-block XOR pre-whitening before AES** — first two bytes of every 16-byte block are XORed with the packet's 16-bit sequence number, same mask all blocks. Source: `clsOmniLinkConnection.cs:396-401`.
- Located a latent bug in PC Access itself: a `LargeVocabulary` skip-path uses a buffer sized for the non-LargeVocabulary case. Harmless on every shipping panel (the count check always satisfies the constraint) but documented in `pca-re/notes/body_parser.md`.

### Library — `omni_pca`

- `crypto.py` — AES-128-ECB with PaddingMode.Zeros semantics, `derive_session_key()`, per-block XOR pre-whitening, `encrypt_message_payload()`/`decrypt_message_payload()`. All citations to C# source line numbers.
- `opcodes.py` — Three IntEnums byte-exact to the C# decompilation: `PacketType` (12 values), `OmniLinkMessageType` (104 v1 opcodes), `OmniLink2MessageType` (83 v2 opcodes). Plus `ConnectionType`, `ProtocolVersion`.
- `packet.py` / `message.py` — Outer `Packet` (4-byte header + payload) and inner `Message` framing. CRC-16/MODBUS (poly `0xA001`).
- `pca_file.py` — Borland LCG XOR cipher, `PcaReader` with `u8/u16/u32/string8/string8_fixed/string16/string16_fixed`, `parse_pca01_cfg()`, `parse_pca_file()`. Account-info fields default `repr=False` to avoid accidental PII leakage in logs.
- `connection.py` — `OmniConnection`: async TCP, full secure-session handshake (4 packets), monotonic per-direction sequence numbers with `0xFFFF → 1` wraparound (skips 0), TCP framing that decrypts the first 16-byte block to learn the inner message length, reader task dispatching solicited replies to Futures and unsolicited messages to a queue, automatic reconnect on `OmniConnectionError`, custom exceptions (`HandshakeError`, `InvalidEncryptionKeyError`, `ProtocolError`, `RequestTimeoutError`).
- `models.py` — 21 typed frozen-slots dataclasses for every Omni object: `SystemInformation`, `SystemStatus`, `ZoneProperties/Status`, `UnitProperties/Status`, `AreaProperties/Status`, `ThermostatProperties/Status`, `ButtonProperties`, `ProgramProperties`, `CodeProperties`, `MessageProperties`, `AuxSensorStatus`, `AudioZoneProperties/Status`, `AudioSourceProperties/Status`, `UserSettingProperties/Status`. Plus `SecurityMode`, `HvacMode`, `FanMode`, `HoldMode`, `ZoneType`, `ObjectType` enums and temperature converters (Omni's linear `°F = round(raw * 9/10) - 40`).
- `commands.py` — `Command` IntEnum (64 values, sourced from `enuUnitCommand.cs` which is the canonical command enum despite the misleading name), `SecurityCommandResponse`, `CommandFailedError`.
- `client.py` — High-level `OmniClient` with 18 methods: `get_system_information`, `get_system_status`, `get_object_properties`, `list_*_names`, `execute_security_command`, `execute_command`, `get_object_status`, `get_extended_status`, `acknowledge_alerts`, typed wrappers (`turn_unit_on/off`, `set_unit_level`, `bypass_zone/restore_zone`, `set_thermostat_{system,fan,hold}_mode`, `set_thermostat_{heat,cool}_setpoint_raw`, `execute_button`, `execute_program`, `show_message`, `clear_message`), `events()` async iterator over typed `SystemEvent` objects.
- `events.py` — `SystemEvent` hierarchy. 26 typed subclasses (`ZoneStateChanged`, `UnitStateChanged`, `ArmingChanged`, `AlarmActivated/Cleared`, `AcLost/Restored`, `BatteryLow/Restored`, `UserMacroButton`, `PhoneLineDead/Restored`, …) + `UnknownEvent` catch-all. SystemEvents (opcode 55) packets carry multiple events; `parse_events()` returns a list. `EventStream` flattens batches across messages.
- `mock_panel.py` — Stateful async TCP server emulating an Omni Pro II controller. Handles handshake, `RequestSystemInformation/Status`, `RequestProperties` for Zone/Unit/Area/Thermostat/Button, `RequestStatus`/`RequestExtendedStatus`, `Command`, `ExecuteSecurityCommand`, `AcknowledgeAlerts`. State changes push synthesized `SystemEvents` packets back to the client.
- `__main__.py` — CLI: `omni-pca decode-pca <file> [--field controller_key|host|port] [--include-pii]`, `omni-pca mock-panel`, `omni-pca version`. PII opt-in.

### Home Assistant integration — `custom_components/omni_pca/`

- `coordinator.py` — `OmniDataUpdateCoordinator` with long-lived `OmniClient`, one-time discovery pass at first refresh (enumerates zones, units, areas, thermostats, buttons), periodic 30s poll for live state, background event-listener task consuming `client.events()` and patching state in-place on each push. `ConfigEntryAuthFailed` on `InvalidEncryptionKeyError` triggers HA's reauth flow.
- Eight platforms wrapping the library client:
  - `alarm_control_panel` — one per area, supports Day/Night/Away/Vacation/DayInstant arm modes with code validation
  - `binary_sensor` — one per binary zone (state + bypass diagnostic) plus 3 system-level (AC, battery, trouble)
  - `button` — one per panel button macro
  - `climate` — one per thermostat (OFF/HEAT/COOL/HEAT_COOL + fan + preset modes)
  - `event` — one per panel, relays 12 typed event types to HA automations
  - `light` — one per unit (dimmable; non-dimmable relays silently ignore brightness)
  - `sensor` — analog zones (temperature/humidity/power), per-thermostat diagnostic temp/humidity/outdoor sensors, panel model+firmware sensor, last-event sensor
  - `switch` — per-zone bypass control (config entity_category)
- `config_flow.py` — User + reauth steps. Host/port/controller_key with hex validation. Probes the panel via `OmniClient.get_system_information()` before creating the entry; surfaces auth/cannot_connect errors with HA-friendly strings.
- `services.yaml` + `services.py` — 7 services (`bypass_zone`, `restore_zone`, `execute_program`, `show_message`, `clear_message`, `acknowledge_alerts`, `send_command`). Idempotent registration; each takes a `config_entry` selector so users pick the panel.
- `diagnostics.py` — Snapshot dump with controller key redacted and zone/unit/area names sha256-hashed.
- `helpers.py` — Pure functions for everything HA-shape-dependent: zone-type→device-class, brightness conversion, HVAC mode round-trip, temperature inverse, alarm state translation, event-type strings. No `homeassistant.*` imports; 61 unit tests covering it.
- `manifest.json` — `iot_class: local_push`, `version: 2026.5.10`, `config_flow: true`, requires `omni-pca==2026.5.10`.
- `hacs.json` at project root for HACS distribution.

### Tests

- **351 passing, 1 skipped.** Ruff clean across `src/`, `tests/`, `custom_components/`.
- 17 e2e tests connecting `OmniClient` to `MockPanel` over real TCP, proving the full handshake + encryption + framing stack roundtrips.
- 12 HA-side integration tests using `pytest-homeassistant-custom-component` — boot HA in-process, drive the config flow, exercise services, verify state mutations. Full HA-side suite runs in <1 second.
- 61 unit tests on `custom_components/omni_pca/helpers.py` running without HA installed.
- Unit tests for every library module (crypto KAT vectors, CRC-16, packet/message ser-de, .pca decrypt, command payloads, event parsing).

### Developer tooling

- `dev/docker-compose.yml` + `dev/Makefile` — One-command HA + MockPanel stack for manual smoke testing and screenshot capture.
- `dev/run_mock_panel.py` — Long-running mock seeded with 5 zones, 4 units, 2 areas, 2 thermostats, 3 buttons, 2 user codes.
- `dev/screenshot.py` — End-to-end automated demo: onboards HA via REST, adds the integration via config-flow API, drives headless chromium via playwright to capture six deep-linked PNGs (overview, integrations list, integration detail, device page, entities table, developer states).

### Documentation

- `docs/JOURNEY.md` — 6,000+ word raw chronological narrative from "pile of binaries" through "351 tests green, screenshots captured". Source material for future writeups.
- `pca-re/notes/findings.md` — RE technical findings (cipher, file format, protocol overview).
- `pca-re/notes/handshake.md` — Byte-level handshake spec with C# source line citations.
- `pca-re/notes/body_parser.md` — .pca body schema + the LargeVocabulary latent bug.
- Top-level `README.md` — Library + HA quick start.
- `custom_components/omni_pca/README.md` — Entity table, services list, automation example, troubleshooting.
- `dev/README.md` — Docker dev stack walkthrough.

### Known gaps

- **Live panel validation**: blocked on the user's panel's Ethernet module being enabled. Mock panel proves the stack roundtrips; the live lap is one TCP connect away once the panel is reachable.
- **Programs discovery**: the library's v1.0 has no `RequestProperties` path for Program objects; the HA coordinator returns an empty programs dict. Programs can still be executed by index via the `omni_pca.execute_program` service.
- **PyPI publish**: `omni-pca` not yet on PyPI; HA `manifest.json` requirements line will only resolve once it is. For now users either install the wheel manually or pip-install from a Git URL.
- **HACS submission**: pending live-panel validation.

[2026.5.16]: https://github.com/rsp2k/omni-pca/releases/tag/v2026.5.16
[2026.5.14]: https://github.com/rsp2k/omni-pca/releases/tag/v2026.5.14
[2026.5.10]: https://github.com/rsp2k/omni-pca/releases/tag/v2026.5.10
