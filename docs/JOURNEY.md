# JOURNEY

Raw chronological notes from a few days reverse-engineering HAI's PC Access
3.17, then writing a Python library and a Home Assistant integration to
talk to the panel directly. Dated. Append-only-ish.

---

## 2026-05-10 morning — the pile of binaries

Started with a directory called `PC Access/` that had clearly been zipped
up off a Mac and handed around. The giveaway was `._*` files next to every
real file:

```
-rw------- 1 kdm kdm     120 Aug 15  2016 ._Newtonsoft.Json.dll
-rw------- 1 kdm kdm  484352 Aug 15  2016 Newtonsoft.Json.dll
```

That's AppleDouble cruft: macOS extended attributes shimmed into companion
files when an HFS+ volume gets archived to a non-Apple filesystem. 120 bytes
of resource fork garbage per real file. Useless. Touched everything from
the PC Access install date (Mar 2018) all the way back to a 2006 firmware
updater. Whoever extracted this had been carrying it across Macs for years.

What we actually had:

| File | Size | What it is |
|------|-----:|-----|
| `PCA3U_EN.exe` | 5.4 MB | The PC Access GUI, a .NET assembly (v3.17.0.843, 2018-01-02) |
| `PCA1106W.exe` | 3.3 MB | Older native C++ version from 2008 |
| `f_update.exe` | 437 KB | Native firmware updater (2006) |
| `OT7FileUploaderLib.dll` | 16 KB | OmniTouch 7 firmware uploader |
| `Our House.pca` | 144 KB | A panel config file. High entropy. Not ours. |
| `PCA01.CFG` | 318 B | App settings. Also encrypted. |
| `Serial Number.txt` | 20 B | A 20-char license key |

`Our House.pca` was the interesting one. Entropy 7.994 bits per byte —
either compressed, encrypted, or both. No magic bytes. No structure
visible in the first 256 bytes. It also had someone else's account name
embedded in the metadata: this panel had been bought used and shipped
with the previous owner's config still on it. Held that thought.

`file PCA3U_EN.exe` came back with `Mono/.Net assembly`. That was the
single biggest piece of luck in the whole project: a .NET assembly means
ilspycmd will give us back readable C# in seconds. Beats staring at IDA
listings of Borland C++ runtime stubs all afternoon, which is what
`PCA1106W.exe` would have made us do.

## 2026-05-10 — decompile and skim

Ran ilspycmd 10.0.1.8346 over `PCA3U_EN.exe`. 898 typedefs. They cleanly
split into two namespaces:

- `HAI_Shared` — the domain model, the wire protocol, the crypto, all of
  it reusable across HAI's product line (Omni, Lumina, HMS).
- `PCAccess3` — just UI. Forms, controls, window positions.

That's the prize: `HAI_Shared` is essentially a free protocol
implementation library, written by people who actually know how the panel
works, sitting there in C# waiting to be read.

First skim of `HAI_Shared`:

- `clsOmniLinkPacket` — outer transport packet. 4-byte header
  (`[seq_hi][seq_lo][type][reserved=0]`) + payload. Sequence number is
  big-endian. There are 12 packet types: NewSession, AckNewSession,
  RequestSecureSession, AckSecureSession, two flavors of
  SessionTerminated, the `OmniLinkMessage` (encrypted, v1) and
  `OmniLink2Message` (encrypted, v2) wrappers, plus their unencrypted
  twins.
- `clsOmniLinkMessage` — inner application message.
  `[StartChar][MessageLength][...payload, payload[0]=opcode...][CRC_lo][CRC_hi]`.
  CRC is CRC-16/MODBUS with poly `0xA001`. Standard.
- `clsAES` — the panel's symmetric crypto. AES-128, ECB,
  `PaddingMode.Zeros`, key reused as IV (which is fine in ECB but a code
  smell that hints at someone copy-pasting from a textbook).
- `enuOmniLink2MessageType` — 83 v2 opcodes. Login, Logout,
  RequestSystemInformation, RequestExtendedStatus, Command, ZigBee
  pass-through, firmware upload, etc.
- `clsCapOMNI_PRO_II`, `clsCapLUMINA`, `clsCapHMS950e`, … — per-model
  capability classes carrying constants like `numZones=176`,
  `numUnits=511`. Real domain model, not a config file.

Wrote those down in `findings.md` and pushed on.

## 2026-05-10 — the cipher that wasn't AES

Then we hit the file format. The `.pca` and `.CFG` blobs *look* like
AES-CBC ciphertext. They aren't. From `clsPcaCryptFileStream`:

```csharp
private byte oldRandom(byte max) {
    RandomSeed = RandomSeed * 134775813 + 1;
    return (byte)((RandomSeed >> 16) % max);
}
// per byte: ciphertext = plaintext ^ oldRandom(255)   // mod 255, not 256
```

That multiplier — `134775813` = `0x08088405` — is the Borland Delphi /
Turbo Pascal `Random()` LCG. So someone wrote this thing in Delphi
originally, ported it to C#, and kept the exact same PRNG so existing
.pca files would still decrypt. The mod-255 (not 256) stays in too,
which means the keystream byte is in `[0..254]`, never `0xFF`. It
doesn't lose information — it just shifts the output distribution.
Quirky but not broken.

Two hardcoded 32-bit keys live in `clsPcaCfg`:

```csharp
private readonly uint keyPC01   = 338847091u;  // 0x142A3D33 — for PCA01.CFG
public  readonly uint keyExport = 391549495u;  // for exported .pca files
```

And a third path: `SetSecurityStamp(string S)` derives a per-installation
key from a stamp string:

```csharp
uint num = 305419896u;   // 0x12345678 — developer Easter egg as init value
foreach (char c in S)
    num = ((num ^ c) << 7) ^ c;
Key = num;
```

`0x12345678` as an init constant is the giveaway: someone was bored at
the keyboard the day they wrote this. It's the kind of thing you grep
for. (The actual hash function, `((k ^ c) << 7) ^ c`, is fine — not
cryptographic, but fine for "let me derive a per-install key from a
serial number.")

## 2026-05-10 — the wrong-key-looks-right problem

Wrote a Python decryptor in maybe an hour: a generator that yields
keystream bytes, an XOR over the file. Easy.

Then we hit a subtle thing. The first script auto-tried the two known
keys and picked the one whose plaintext "looked more printable". It
picked `keyExport`, ran the parser, and got nonsense — but a *plausible*
kind of nonsense: short non-empty strings, non-zero counter values,
generally the texture of real binary data.

Turns out **printable-character ratio is a terrible heuristic for binary
file plaintext.** Random noise is, on average, slightly more "printable"
than a real binary file padded with zeros and length-prefixed strings —
because random noise has a uniform distribution and a real file has long
runs of `0x00` (which falls outside the 32–127 printable range).

Replaced it with something concrete and stupid:

```python
def score(pt):
    n = pt[0]
    if not (1 <= n <= 64): return 0
    tag = pt[1:1+n]
    if all(32 <= b < 127 for b in tag):
        return 100 + n
    return 0
```

The first byte is a String8 length, and the next `n` bytes should be the
ASCII version tag like `CFG05` or `PCA03`. If it parses cleanly, the key
is right; if not, it isn't. Robust because it's not statistical.

`PCA01.CFG` decrypted with `keyPC01`. First bytes:

```
00000000  05 43 46 47 30 35 17 41 ...    .CFG05.A
```

`CFG05`. Format version 5. Walked the rest of the schema (modem strings,
port number, key field, password) and pulled out the prize:

```
pca_key = 0xC1A280B2  (3,248,652,466)
password = "PASSWORD"   # factory default, never changed
```

So the per-installation `.pca` key was sitting inside `PCA01.CFG` the
whole time, encrypted with a hardcoded key that's right there in the
binary. The `keyExport` path is only for files that were exported for
sharing, which is *not* what `Our House.pca` was — it was the live
in-place config.

Decrypted `Our House.pca` with `0xC1A280B2`. First bytes:

```
00000000  05 50 43 41 30 33 ...     .PCA03
```

`PCA03`. File format v3. Right key.

## 2026-05-10 — the 2191-byte header parses byte-perfect

Read `clsHAC.ReadFileHeader` to figure out the layout:

```
String8         version_tag         "PCA03"
String8(30)     AccountName
String16(120)   AccountAddress
String8(20)     AccountPhone
String8(4)      AccountCode
String16(2000)  AccountRemarks
byte            Model
byte            MajorVersion
byte            MinorVersion
sbyte           Revision
```

One thing about `ReadString8(out S, byte L)`: it always consumes
`1 + L` bytes regardless of the declared string length. So the strings
are fixed-width slots with a length prefix, not variable-length.

Total header size: 2191 bytes.

Then we found the validation block at `clsHAC.cs:7943`:

```csharp
if (num == 2191) { /* header read OK */ }
```

If your byte counter doesn't equal 2191 after parsing the header, you
got it wrong. It did. That was the moment we knew the parser was
correct: not by inspection of the output, but by hitting an exact magic
number that the original code was checking against.

Decoded header:

- Model byte = `0x10` = `enuModel.OMNI_PRO_II`
- Firmware: 2.12 r1
- AccountName / Address / Phone — the previous owner's PII
- 8 user codes, all still factory default `12345678`

That last one stung. The panel had probably been sitting on someone's
wall for a decade with `12345678` as the master code. (Not our panel,
yet — but our panel was about to inherit it.) Plaintext stays in
`extracted/Our_House.pca.plain` and that path stays in `.gitignore`.
All future notes redact PII.

## 2026-05-10 — walking the body

Header was 2191 bytes; the file is 144 KB. Plenty more to parse before
we'd hit the network connection block where the AES key for live-panel
talk is stored.

The body layout (from `clsHAC.ReadFromFile`):

```
ByteArray       SetupData.data            (3840 bytes for OMNI_PRO_II)
bool            slRequireCodeForSecurity
bool            slPasswordOnRestore
UInt16          (discarded)
UInt16          EventLog.Count
UInt32          (discarded)
ZoneNames, UnitNames, ButtonNames, CodeNames, ThermostatNames,
    AreaNames, MessageNames
ZoneVoices, UnitVoices, ButtonVoices, CodeVoices, ThermostatVoices,
    AreaVoices, MessageVoices
Programs
EventLog
# v >= 2:
if Ethernet feature:
    String8(120)   Connection.NetworkAddress
    String8(5)     port-string
    String8(32)    ControllerKey-as-hex   <- 32 hex chars = 16-byte AES key
...
```

The Names blocks were straightforward: each is `max_slots * (1 + name_len)`
bytes. For Zones that's `176 * 16 = 2816` bytes. Adds up cleanly.

Then we hit the Voices blocks and the parser desynced.

## 2026-05-10 — the latent bug in PC Access itself

Each "Voice" block lets the panel speak the name of an object. Six
phrases per object (`numVoicePhrases = 6`). The C# reads them like this:

```csharp
byte[] B = new byte[CAP.numVoicePhrases];      // 6 bytes
for (int i = 1; i <= GetFileMaxX(); i++) {
    num = (i > Count)
        ? num + FS.ReadByteArray(out B, B.Length)   // skip path: 6 bytes
        : num + _Items[i-1].Voice.Read(FS);         // structured path
}
```

The "structured path" calls `clsVoiceWordArray.Read`, which branches on
whether the panel has the `LargeVocabulary` feature:

- LargeVocabulary present → 6 phrases × **2 bytes** (UInt16) = **12 bytes**
- LargeVocabulary absent → 6 phrases × 1 byte = 6 bytes

OMNI_PRO_II *has* LargeVocabulary. So the structured path reads 12 bytes
per slot. But the **skip path** in the loop above always reads 6 bytes,
no matter what. There's no `if (LargeVocabulary) B = new byte[12];`.

If `Count == GetFileMaxX()` (every slot is filled), this never matters —
the skip path is never taken. For every block on our panel except one,
that's true. But Units has `Count = 511` and `GetFileMaxX = 512`, so
exactly one slot takes the skip path, reads 6 bytes when it should have
read 12, and the next 6 bytes — which are actually the start of the
*next* block — get treated as the tail of the current slot. The parser
walks 6 bytes off the rails and never recovers.

The C# code in the wild gets away with this because `Count >= Max` for
basically all real panels in deployment. But it's a real bug — it would
bite if a model ever shipped with LargeVocabulary AND had Buttons or
Messages with `Count < Max`. We patched our parser; the original is
still wrong.

Found it by hex-dumping the file, locating the panel IP address
(`192.168.1.9`) at byte offset `0xe2d8`, and back-solving the diff
between where we expected to land and where the IP actually was. The
gap was exactly 6684 bytes, which is `(512-1)*6` worth of voice slots
read at half the right size. Math checked out. Off by N.

## 2026-05-10 — the prize

After the Voices, the body has Programs (1500 × 14 B), EventLog (250 ×
9 B), and then — for a v3 file with the Ethernet feature — the
Connection block:

```
String8(120)   Connection.NetworkAddress
String8(5)     port-string
String8(32)    ControllerKey-as-hex
```

For our panel:
- IP: `192.168.1.9`
- Port: `4369`
- ControllerKey: 16 bytes of AES-128 key, extracted at file offset
  `0xe2d8`

Total bytes to that point: `2191 + 3840 + 10 + 15407 + 13374 + 21000 + 2250 = 58072 = 0xe2d8`.
Exactly the offset where the IP appears in the hex dump. Done.

That key plus the right handshake = direct talk to the panel.

## 2026-05-10 — the two non-public quirks

Now we needed to read `clsOmniLinkConnection.cs`. It's 2109 lines of
state machine for the secure-session handshake, the keepalive timer, the
TCP framing, and the encryption. We expected a textbook AES session: send
client-hello, get server-hello, derive key from PIN somehow, encrypt
everything from then on.

What we found instead were two surprises that no public Omni-Link
write-up we'd seen mentions. Both of them look like quirks. Both of them
will reject your client with `ControllerSessionTerminated` if you skip
them.

### Quirk 1 — the session key is not the ControllerKey

You'd expect the AES session key to be the ControllerKey verbatim. It
isn't. From `clsOmniLinkConnection.cs:1886-1892`:

```csharp
SessionKey = new byte[16];
ControllerKey.CopyTo(SessionKey, 0);
for (int j = 0; j < 5; j++)
{
    SessionKey[11 + j] = (byte)(ControllerKey[11 + j] ^ SessionID[j]);
}
AES = new clsAES(SessionKey);
```

The first 11 bytes of the session key are the ControllerKey verbatim.
The last 5 bytes are the ControllerKey XORed with a 5-byte `SessionID`
nonce that the controller sent in `ControllerAckNewSession`. That's
the entire key derivation. No PBKDF2, no HKDF, no PIN, no salt. Just
five bytes of XOR.

The same five-byte block appears twice in the source — once for UDP
(line 1423) and once for TCP (line 1886). Identical.

The implication for someone writing a client is: if you encrypt your
`ClientRequestSecureSession` with the raw ControllerKey, the panel
decrypts it to garbage and disconnects you. You have to wait for the
nonce, mix it in, *then* encrypt.

### Quirk 2 — per-block XOR pre-whitening before AES

This one is the real headline. Before AES-encrypting any payload block,
the first two bytes of every 16-byte block get XORed with the packet's
sequence number. Same XOR mask, every block of the packet. From
`clsOmniLinkConnection.cs:396-401`:

```csharp
for (num = 0; num < PKT.Data.Length; num += 16)
{
    PKT.Data[num]     = (byte)(PKT.Data[num]     ^ ((PKT.SequenceNumber & 0xFF00) >> 8));
    PKT.Data[num + 1] = (byte)(PKT.Data[num + 1] ^  (PKT.SequenceNumber & 0xFF));
}
PKT.Data = AES.Encrypt(PKT.Data);
```

And then the inverse on receive (`:413-417`):

```csharp
PKT.Data = AES.Decrypt(PKT.Data);
for (int i = 0; i < PKT.Data.Length; i += 16)
{
    PKT.Data[i]     = (byte)(PKT.Data[i]     ^ ((PKT.SequenceNumber & 0xFF00) >> 8));
    PKT.Data[i + 1] = (byte)(PKT.Data[i + 1] ^  (PKT.SequenceNumber & 0xFF));
}
```

So the on-the-wire encryption is "AES-128-ECB of (payload XOR-prewhitened
with the seq number, two bytes per block)". A naive Omni-Link client that
just AES-ECB-encrypts the raw payload will produce ciphertext the panel
won't accept.

It feels weak — an attacker with a known-plaintext for one block can
recover the seq XOR mask trivially, and from there the whitening is
unprotected. But it's the protocol. The panel won't talk to you without
it.

We think the original intent might have been something like nonce-mixing
(use the seq as a per-packet salt to defeat ECB block-repetition
attacks), and the implementation got cargo-culted from one block to all
blocks of the packet. Doesn't matter. Implement it. Move on.

A bonus surprise: **there is no separate `Login` step on TCP.** The C#
defines `clsOL2MsgLogin` (v2 Login, opcode 42) but never instantiates
it on the TCP path. Possessing the right ControllerKey *is* the
authentication. The login opcode appears to be a serial-only artifact
from before the Ethernet module existed. The v1 serial path *does*
construct `clsOLMsgLogin` with the user's PIN; the v2 TCP path goes
straight from `ControllerAckSecureSession` to `RequestSystemInformation`.

We documented all of this in `notes/handshake.md` while it was fresh.

## 2026-05-10 around noon — first commit

```
9a02418 Initial scaffold + protocol primitives
```

uv project, ruff, pytest, mypy strict, MIT, README, gitignore explicitly
protecting any `.pca` or panel keys. Date-versioned (CalVer): `2026.5.10`.
The library lives in `src/omni_pca/`:

- `crypto.py` — AES-128-ECB plus the per-block XOR seq pre-whitening and
  the `SessionKey = CK[0:11] || (CK[11:16] XOR SessionID)` derivation
- `opcodes.py` — all 12 packet types, all 104 v1 opcodes, all 83 v2
  opcodes, all transcribed by hand from the decompiled enums
- `packet.py` — outer `Packet` with `encode()`/`decode()`
- `message.py` — inner `Message` with CRC-16/MODBUS
- `pca_file.py` — Borland LCG cipher, `PcaReader`, parsers for both
  `.pca` and `.CFG`

49 tests passed, ruff clean. The protocol unit tests use canned bytes
extracted from the C# source; they don't need a panel to run.

## 2026-05-10 1pm — mock panel as ground truth

Second commit:

```
1901d6e Async client + mock panel + e2e roundtrip
```

The async client (`OmniConnection`, `OmniClient`) runs the four-step
secure-session handshake, frames TCP correctly (read first 16-byte block,
decrypt, learn `MessageLength`, read the rest), keeps a per-direction
monotonic sequence number that wraps `0xFFFF → 1` (skipping 0 because the
controller uses 0 for unsolicited packets), and dispatches solicited
replies to a Future while shoving unsolicited packets into a queue.

That's all well and good, but how do we test it without a panel? The
panel was at `192.168.1.9` last we knew, and we had no idea if its
network module was even on. Building a real Omni controller emulator
in Python turned out to be the right answer.

`mock_panel.py` is a TCP server that:

- accepts `ClientRequestNewSession`, generates a 5-byte SessionID,
  sends back `ControllerAckNewSession` with the version bytes `00 01`
  prepended
- derives the same SessionKey the client did (using the same XOR-mix)
- decrypts the `ClientRequestSecureSession`, validates that the 5-byte
  echo matches the SessionID it just sent, sends back the symmetric
  `ControllerAckSecureSession` (re-encrypting the same SessionID)
- handles `RequestSystemInformation`, `RequestSystemStatus`,
  `RequestProperties` (Zone/Unit/Area, both absolute index and rel=1
  iteration with EOD termination), and Naks anything else

It's a thin emulator but it's a *complete* protocol counterpart. Six
end-to-end tests connect a real `OmniClient` over a real TCP socket to
a real `MockPanel` and exchange real frames. They prove the handshake,
the AES, the XOR whitening, and the sequence numbering all agree —
because if any one of them is wrong, decryption produces garbage and
the connection drops.

That ground-truth check was load-bearing. It meant we could iterate on
the client all afternoon without worrying that some bug in our
encryption was being masked by a bug in our framing.

## 2026-05-10 ~1:10pm — the HA scaffold

Third commit:

```
2e43936 HA custom_component scaffold (binary_sensor for zones)
```

Drop-in Home Assistant integration at `custom_components/omni_pca/`:
manifest, config_flow with auth + reauth, coordinator with reconnect
logic, binary_sensor for each named zone with `device_class` derived
from `zone_type` (OPENING, MOTION, SMOKE, etc.). 12 unit tests for
`parse_controller_key()` because that's the one piece of pure logic
worth pinning down hard.

Status of the HA component itself wasn't validated against a running
Home Assistant — that comes next. But the HACS manifest is there, so
once we trust it we can drop it in.

## 2026-05-10 2pm — fleshing out the model surface

Fourth commit:

```
08974e2 Models: 16 status/properties dataclasses + enums + temp converters
```

The Omni protocol has a wide object surface — Zones, Units, Areas,
Thermostats, Buttons, Programs, Codes, Messages, Aux Sensors, Audio
Zones, Audio Sources, User Settings — and each has both a "properties"
record (configured, mostly static) and a "status" record (live state).

Wrote frozen-slots dataclasses for all of them, with `.parse(payload)`
classmethods that decode the byte layouts straight from the C# field
definitions. Added IntEnums for the dispatch tags (`ObjectType`,
`SecurityMode`, `HvacMode`, `FanMode`, `HoldMode`, `ThermostatKind`,
`ZoneType`, `UserSettingKind`).

One small surprise from `clsText.cs`: the temperature encoding the
panel uses is *linear*, not the non-linear thermistor scale we'd
guessed it might be. `C = raw / 2 - 40`. Easy.

42 new tests. 139 total.

## 2026-05-10 ~2:15pm — commands and events

Fifth commit:

```
68cf44a Library v1.0 phase B: command opcodes + typed system events
```

`commands.py` — the `Command` IntEnum, sourced from `enuUnitCommand.cs`
which is the canonical "all commands" enum despite the misleading name
(it covers HVAC, security, scene, button, message commands too — not
just units). One naming weirdness: `enuUnitCommand.UserSetting` (104) is
actually EXECUTE_PROGRAM. Renamed for clarity in our enum and left the
original C# alias documented inline so anyone cross-referencing won't
get confused.

`OmniClient` got 18 new methods: `execute_command`,
`execute_security_command`, `acknowledge_alerts`, `get_object_status`,
`get_extended_status`, plus convenience wrappers (`turn_unit_on`,
`set_unit_level`, `bypass_zone`, `set_thermostat_heat_setpoint_raw`,
…). All the command methods raise `CommandFailedError` on Nak.

`events.py` — the `SystemEvents` (opcode 55) decoder. The panel pushes
batches of these unsolicited; each batch contains multiple events of
different types (zone state changes, unit state changes, arming
changes, alarm activated, AC lost, battery low, phone line dead, X10
codes received, …). 28 dispatch tags, 26 typed event subclasses, an
`UnknownEvent` catch-all for opcode values we don't know yet, and an
`EventStream` helper that flattens batches across messages.

55 new tests. 194 total.

## 2026-05-10 ~2:30pm — stateful mock and the full v1.0 surface

Sixth commit:

```
c26db62 Library v1.0 phase C: stateful mock + e2e for the new surface
```

The mock got real state. `MockUnitState`, `MockAreaState`, `MockZoneState`,
`MockThermostatState`, plus a `user_codes` table for security validation.
All the new opcodes wired through:

- `Command` (20) → Ack with state mutation, dispatching UNIT_ON, UNIT_OFF,
  UNIT_LEVEL, BYPASS_ZONE, RESTORE_ZONE, SET_THERMOSTAT_HEAT, etc.
- `ExecuteSecurityCommand` (74) → Ack on a valid code, Nak on invalid
- `RequestStatus` (34) → `Status` (35) for the four object kinds with
  hard-coded record sizes per `clsOL2MsgStatus.cs:13-27`
- `RequestExtendedStatus` (58) → `ExtendedStatus` (59) with the
  `object_length` prefix and the richer per-type fields
- `AcknowledgeAlerts` (60) → Ack
- And synthesized `SystemEvents` (55) pushed with `seq=0` whenever state
  changes, so the e2e tests can subscribe to events through the real
  client API and watch them roundtrip cleanly through `events.parse_events()`

9 new e2e tests — arm/disarm with code validation, unit on/off/level,
zone bypass/restore, thermostat setpoint, push events for arming and
unit changes, acknowledge_alerts. 203 total passing, 2 skipped (the
HA harness and a `.pca` fixture we don't ship).

The library has the v1.0 surface: read, command, status, extended status,
events. All exercised by an in-process emulator that speaks the same
protocol as the real panel.

## 2026-05-10 afternoon — trying to find the real panel

Now the part that didn't go well.

The `.pca` file said the panel lived at `192.168.1.9:4369`. Tried to
connect: nothing. TCP SYN, no SYN-ACK. Pinged: silent. nmap'd the
subnet to make sure we were on the right network:

- `192.168.1.7`, `.8`, `.11` — open ports including SSH with banner
  `SSH-2.0-dropbear_2018.76`. Three OmniTouch 7 touchscreens. They're
  the wall-mounted controllers; they live on the same LAN as the panel,
  speak Omni-Link II to the panel themselves, and run a stripped Linux
  with dropbear for the firmware updater. Confirmed by the SSH banner
  date (2018) lining up with the OmniTouch 7 firmware era.
- `.6` — likely the panel itself, but no open ports, no response.
- `.9` — also dark. The 2018 IP either changed or the network module
  was disabled at some point.

So the panel is sitting there, doing its job (the touchscreens clearly
work — they're on the network), but its Ethernet/Omni-Link II module is
either turned off in the panel's setup menu or the network bridge
hardware is bad. We have the ControllerKey, we have the right port, we
have a fully-tested client and a mock panel that proves the client
works end-to-end — but we can't prove it against the real thing yet.

We have, in other words, built the world's most thoroughly-tested
unused integration. There is something quietly funny about that.

The fix is physical: walk over to the panel, find the menu that
enables the Ethernet module, save, reboot. Then the live validation
becomes a five-minute test. Until then, the mock is the best we have,
and the mock is a faithful enough emulator that we trust it.

## What's next

The Home Assistant custom_component is being rebuilt on top of the v1.0
library surface — alarm_control_panel, light, switch, climate, sensor,
scene, button, event entities, plus services.yaml and diagnostics. That
work is in progress and will be validated as soon as we can bring the
panel's network module online.

When we do, the moment of truth is one TCP connect to port 4369 and
one `RequestSystemInformation` exchange. If it comes back with
`Omni Pro II / 2.12 r1`, the entire stack — file decryption, key
extraction, key derivation, XOR pre-whitening, AES, the works — was
right end to end. If it comes back with `ControllerSessionTerminated`,
we missed something subtle. The mock says we didn't. We'll find out.

---

## Things worth remembering

**The "wrong key looks plausible" problem is real and recurring.**
Statistical heuristics (entropy, printable ratio, frequency analysis)
are great for telling random noise from English; they're terrible for
telling random noise from binary file plaintext. When a file format
has a known header magic, parse-the-magic beats every heuristic.

**Magic numbers in source code are gifts.** `0x12345678` as an init
value, `134775813` as an LCG multiplier, `2191` as a header length —
each one is a hard checkpoint that tells you, on first try, whether
the next four hours are going to be productive or not.

**A complete protocol counterpart is worth more than ten times its
LOC in confidence.** The mock panel was maybe 400 lines of code and
it eliminated an entire category of "is the client wrong or am I
holding it wrong" questions. Every test that connects a real client
to it through real TCP is a test that the entire stack — handshake,
encryption, framing, sequencing — agrees with itself.

**Quirk #2 (the per-block XOR pre-whitening) is the kind of thing
nobody finds without doing the work.** It's not in `jomnilinkII`,
not in `pyomnilink`, not in the public Omni-Link II writeups we
checked. The decompiled C# was unambiguous and twice-redundant
(once for encrypt, once for decrypt). Without those exact six lines
of source, an OSS client that did everything else right would still
get `ControllerSessionTerminated` on the first encrypted message,
with no useful diagnostic.

**The latent LargeVocabulary bug in PC Access is harmless but
symptomatic.** It's a copy-paste mistake — the skip path uses a
buffer sized for the no-LargeVocabulary case while the structured
path uses the LargeVocabulary size. Every panel in deployment
satisfies `Count >= Max` for the affected blocks, so the bug never
fires. But it would, on a model that doesn't, and PC Access would
silently mis-parse its own config file. The kind of bug that lives
in shipping code for a decade because nobody runs the unhappy path.
