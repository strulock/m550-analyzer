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
- **Which reply is the car.** Only a reply with the identification control
  word `0x0011` counts as HSFZ; anything else on the port is "unknown" and
  never fills the IP box. Replies from `169.254.x.x` (the ENET link) sort
  first. One car on the link is used straight away; a car on another network,
  or more than one, is shown with its VIN (from `BMWVIN…` in the reply) for
  you to confirm first.
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

- **Diagnosing a connection.** `n63read diag` (or the GUI's journal, see the
  README's *Connection diagnostics*) records the network snapshot, every
  Discover datagram, port checks, the first frames on the wire and a verdict.
  As administrator it can also capture the packets with `pktmon`.
- **Discover requests.** Discover sends the zero-length HSFZ identification
  request BMW's tools use (`00 00 00 00 00 11`), the 2-byte form
  (`00 00 00 02 00 11 00 00`), and a DoIP identification request
  (`02 FD 00 01 00 00 00 00` to UDP 13400) to recognise a DoIP car. With no
  reply it reads the ARP table: the car asks for this PC's MAC before it
  answers, so its address shows up there even if the answer is lost.

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
| `0x22` ReadDataByIdentifier | every live value: `22 <DID hi> <DID lo>`, reply `62 <DID> <data>`; with batching (`log --batch`), up to 8 identifiers in one request |
| `0x19` ReadDTCInformation | fault memory: sub-function `0x02` (reportDTCByStatusMask), mask `0xFF`; per fault, `0x04` (snapshots) and `0x06` (extended data) |
| `0x3E` TesterPresent | the module scan (`3E 00`): does anything answer at this address? |

Never sent: `0x2E` (write), `0x2F` (I/O control), `0x31` (routines),
`0x14` (clear faults), `0x2C` (define data identifier, which writes into ECU
RAM). The SGBDs define hundreds of jobs using these. The catalogs keep only
the `0x22`-readable entries, and a catalog row is only ever read with
`0x22`. A job like `SVK_LESEN` that BMW lists as "22;2E" is still only read.

## Two connections

The gateway takes two diagnostic connections at once, each from its own
tester address. A third is reset at once (`WinError 10054`). Two from the
same address knock one of them off without an error: its replies just stop.
(Seen on the car, October 2026.)

n63read's `Tester` uses them like this:

| Connection | Tester address | Carries |
|---|---|---|
| 1 | `0xF4` | the master DME `0x12` and every other module |
| 2 | `0xF5` | the slave DME `0x13` (bank 2), opened on its first request and closed after 10 s unused |

Each request names its module, so one connection reaches any number of
modules; bank 2 has its own connection by design.

Other n63read processes don't connect while one holds the car: the app
(while connected) or a running `n63read log` lends its connections through
`n63read.share`, a localhost port whose number and token are in
`%APPDATA%\n63read\tester.json`. The CLI and Claude's tools borrow it if it's
there and connect themselves only if it isn't (Claude's as `0xF6`/`0xF7`).
Borrowed requests go through the same read-only check.

## Fault memory (0x19)

`19 02 FF` returns `59 02 <availability mask>` then 4-byte records:
`DTC[3] status[1]`. The 3-byte DTC, as 6 hex digits, is the key into
`BMW_fault_code_texts.csv`. The status byte is the ISO 14229 *statusOfDTC*
bitfield (testFailed, pending, confirmed...), and `BMW_dtc_status_byte.csv`
gives BMW's own wording for each combination. `0x05` is "currently present
and stored", `0x04` "not present now, stored".

`FF` as the mask returns every code a module can raise, mostly ones that
never failed (status `0x50`, `0x40`: not tested yet). n63read shows a code
only if it failed: now, this or last cycle, or since the memory was cleared
(`--every-code` lists the rest). The DSC (`0x29`) on this car reports most
of its list as `0x58`: *confirmed* together with *not tested since the
clear*, test entries ("Dummy-Fehlerspeichereintrag") included. That
combination doesn't count as failed.

### What was stored with a fault (0x19 04, 0x19 06)

`19 04 <DTC> FF` returns the fault's snapshots: `59 04 DTC[3] status[1]`,
then per snapshot `number[1] count[1]` and `count` × `identifier[2] data[n]`.
BMW's standard identifiers are the same in every UDS module on this car
(`DTCSNAPSHOTIDENTIFIER` in each module file) and are in
`BMW_freeze_frame_fields.csv`: `0x1700` odometer (3 bytes, km), `0x1701`
vehicle time (4, s), `0x1702` SAE code (3), `0x1731` fault class (1, BMW's
`FEHLERKLASSE` table), `0x1750`/`0x1751` power state (1 and 3; `0x1750` read
with the `UWB_TAB_PWF` table), `0x1768`/`0x1769` extended odometer and time
(4 and 6; the DSC stores `0x1769` in 4). A module's own environment values
may follow. The DME's are in the same file (its `FUMWELTTEXTE`: engine
temperature, pressures, air mass... with their scaling and value tables);
for other modules, nothing after the first own value can be split without
its size, so the rest is shown raw.

`19 06 <DTC> FF` returns the extended data: `59 06 DTC[3] status[1]`, then
`number[1] data[n]` per record. `DTCEXTENDEDDATARECORDNUMBER` gives `0x01`
condition byte, `0x02` HFK (times it occurred) and `0x03` HLZ (healing
counter), one byte each. A module that keeps no snapshots or counters for a
code says `requestOutOfRange`.

### Several identifiers per request (0x22)

ISO 14229 allows `22 <DID> <DID> ...`; the reply is each identifier echoed
with its data. To split it the sizes must be known, so a batch only holds
DME values whose catalog size is exact, and the reply is accepted only if
every echo is an identifier that was asked for, at exactly the place the
sizes put it, with nothing left over. Identifiers the DME doesn't support
are left out of the reply and read singly. A refusal, a timeout or anything
that doesn't line up turns batching off for that DME for the session.
`n63read speed` measures both ways; `n63read log --batch` uses it. Whether
this DME takes it is still to be checked at the car.

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
| Live DME read (0x12, 0x13) | **1 s** (`core.VALUE_TIMEOUT`) | read every cycle, so one that never answers must not stall the log |
| DME identifier that didn't answer | the address is skipped for the rest of that cycle; the identifier is left alone for **30 s** (`VALUE_KEY_BACKOFF`) and, if nothing at that address had answered yet, the whole address for **5 s** (`VALUE_TARGET_BACKOFF`) | a sleeping bank-2 DME costs one timeout, not one per channel |
| DME identifier refused as unsupported (NRC `0x11`, `0x12`, `0x31`) | not asked again this session | it will never answer |
| Other DME request | 2 s socket timeout, up to 5 attempts' worth of wait for pending replies | the DME is the priority |
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
