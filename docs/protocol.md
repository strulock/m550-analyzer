# Protocol: HSFZ over ENET

BMW F- and G-series cars expose diagnostics on the OBD port's Ethernet pins
("ENET"). The protocol on top is **HSFZ** (High Speed Fahrzeugzugang), not
ISO 13400 DoIP, though both carry the same UDS (ISO 14229) requests.

| | HSFZ (this car) | DoIP |
|---|---|---|
| Diagnostic port | TCP 6801 | TCP 13400 |
| Discovery | UDP broadcast to 6811 | UDP 13400 |
| Header | 4-byte length + 2-byte control | 8 bytes |
| Addresses | 1 byte | 2 bytes |
| Routing activation | not needed | required |

Implementation: `core.Hsfz`, `core.discover`.

## Getting connected

- **Adapter.** A USB "ENET cable" is a plain Ethernet adapter. The car runs
  no DHCP, so Windows self-assigns a `169.254.x.x` link-local address after
  30–60 s (or set a static `169.254.100.100/255.255.0.0`, no gateway).
  Ignition on (position 2).
- **Discovery.** `discover()` broadcasts a vehicle identification request
  (length 2, control `0x0011`, payload `00 00`) to `255.255.255.255` and
  `169.254.255.255` on UDP 6811 and collects replies (the gateway answers
  with the car's IP and identification).
- **The broadcast gotcha.** With Wi-Fi up, an unbound broadcast leaves via
  the default route and never reaches the car. Bind to the adapter's
  `169.254.x.x` address (GUI *via*, CLI `--bind`). The reply is also dropped
  unless that network is set to *Private* and Python / the exe is allowed
  through Windows Firewall.
- **WinError 10049, "The requested address is not valid in its context".**
  Discover tried to send from an address the PC doesn't have (any more).
  The adapter picks a new `169.254.x.x` whenever the link resets (unplugged,
  ignition cycled), and a remembered address goes stale. The GUI now
  re-reads the addresses on every Discover and switches to the adapter's
  current one; if there is none, the adapter isn't connected or Windows
  hasn't set it up yet (30–60 s after plugging in).

## Frames

```
[length: u32 BE][control: u16 BE][source: u8][target: u8][UDS payload...]
```

`length` counts source, target and payload (payload length + 2).

| Control | Meaning |
|---|---|
| `0x0001` | diagnostic request / response |
| `0x0002` | acknowledge: the gateway echoes every request back first; skip it |
| `0x0011` | vehicle identification (discovery) |

The tester's source address is `0xF4`. Each module has a 1-byte target
address; see [Modules](modules.md).

`Hsfz.request()` sends one UDS request and waits for the matching reply:

- ack frames (`0x0002`) are skipped;
- a reply must come **from** the target **to** `0xF4`, or it is ignored. A
  late reply from an earlier request, such as the previous address in a
  scan, must not be credited to this one;
- `7F <service> 78` (*responsePending*) means "still working", so it keeps
  reading;
- any other `7F <service> <code>` raises `NegativeResponse`;
- the overall deadline is `timeout × (retries + 1)`; a socket timeout on any
  single read raises `HsfzTimeout`.

## Services: read-only, enforced

`core.ALLOWED_SERVICES = {0x19, 0x22, 0x3E}`; `request()` refuses anything
else before it reaches the wire.

| Service | Used for |
|---|---|
| `0x22` ReadDataByIdentifier | every live value: `22 <DID hi> <DID lo>`, reply `62 <DID> <data>` |
| `0x19` ReadDTCInformation | fault memory: sub-function `0x02` (reportDTCByStatusMask), mask `0xFF` |
| `0x3E` TesterPresent | the module scan (`3E 00`): does anything answer at this address? |

Never sent: `0x2E` (write), `0x2F` (I/O control), `0x31` (routines),
`0x14` (clear faults), `0x2C` (define data identifier, which writes into ECU
RAM). The SGBDs define hundreds of jobs using these. The catalogs keep only
the `0x22`-readable entries, and a catalog row is only ever read with
`0x22`. A job like `SVK_LESEN` that BMW lists as "22;2E" is still only read.

## Fault memory (0x19)

`19 02 FF` returns `59 02 <availability mask>` then 4-byte records:
`DTC[3] status[1]`. The 3-byte DTC, as 6 hex digits, is the key into
`BMW_fault_code_texts.csv`. The status byte is the ISO 14229 *statusOfDTC*
bitfield (testFailed, pending, confirmed...), and `BMW_dtc_status_byte.csv`
gives BMW's own wording for each combination. `0x05` is "currently present
and stored", `0x04` "not present now, stored".

Many fault codes embed the module address: the generic *Energiesparmode
aktiv* (energy-saving mode active) code is `02AA00`, where `AA` is the
module's address (`021200` for the DME, `021800` for the transmission). That
is how a module file's address was confirmed, see
[SGBD format](sgbd-format.md#finding-a-modules-address).

## Negative responses that matter

| Code | Name | Meaning here |
|---|---|---|
| `0x31` | requestOutOfRange | this module has no such identifier. A real answer, not a failure: the app stops asking for it for the session and marks it ✗ |
| `0x11` / `0x12` | serviceNotSupported / subFunctionNotSupported | same treatment as `0x31` |
| `0x22` | conditionsNotCorrect | exists but not now (engine running, wrong session...) |
| `0x33` | securityAccessDenied | needs unlocking, which this tool never does |
| `0x78` | responsePending | wait for the real answer |

## Timeouts

| Situation | Timeout | Why |
|---|---|---|
| Normal read (DME) | 2 s socket timeout, up to 5 attempts' worth of wait for pending replies | the DME is the priority |
| Read from another module | **0.5 s** (`core.MODULE_TIMEOUT`) | a missing or sleeping module would otherwise hold up every DME sample for 2 s |
| Module that didn't answer | left alone for **30 s** (`NO_ANSWER_BACKOFF`) | it may be asleep; try again later |
| Module scan (`3E 00`) | 0.5 s per address (`PROBE_TIMEOUT`), no retries; adjustable as **Probe wait s** in the GUI and `--probe-timeout` in the CLI (0.05–10 s) | most addresses are empty |

## Identifying a module's software

- `22 F1 50` returns a module's **SGBD index** (3 bytes, e.g. `0F24C0`),
  which `G30_module_sgbd_map.csv` maps to the SGBD file. Confirmed on this
  car for the cluster (`0x60` → `0F24C0` → `KOMB_G11`).
- `22 F1 01` returns the **SVK** (software and hardware part list).
- `n63read check` takes a different route: it reads every identifier a
  catalog lists and checks each reply's **size** against the catalog. That
  tests the catalog itself, not just the module's name; see
  [Modules](modules.md#is-this-the-right-file).
