# Architecture

Pure Python standard library at runtime (`socket`, `struct`, `csv`, `json`,
`threading`, `queue`, `tkinter`). PyInstaller builds the two Windows exes.

```
src/n63read/
  core.py       protocol, decoding, catalogs, the reader, the check, answers store
  cli.py        n63read / n63read-cli.exe: discover, scan, read, log, faults, check, gps, selftest
  gui.py        n63read.exe: Tkinter app
  playback.py   demo data and log replay (no Tk, testable)
  data/         catalogs and lookup CSVs (see data-sources.md)
tools/
  module_catalog.py   .prg -> module catalogs + fault texts
  translate.py, glossary.py   German -> English columns
  channel_docs.py     catalogs -> docs/channels/
  sgbd_tables.py      the original directory reader (DME)
tests/          pytest; fake_car.py is an HSFZ stand-in for the car
```

## Reading: `core.ChannelReader`

The GUI poller and the CLI `log` share one reader. Per cycle it is given the
watch list `[(name, did, meta)]` and returns `{name: (value, unit, note)}`:

- **DME channels** (no `MODULE`) are read every cycle from the target
  address, one request each.
- **Other modules' channels** are grouped by `(module, DID)`: one request
  fills every field of a record. They are refreshed on a schedule, not every
  cycle:

| Setting | Value | |
|---|---|---|
| `FAST_PERIOD` | 1 s | identifiers in `FAST_DIDS`: GPS, speeds, gear, transmission temps and shaft speeds, wheel speeds, brake pressures, pedal travel, acceleration, steering angle/torque, ride height, damper currents, xDrive torque, fuel pump |
| `SLOW_PERIOD` | 10 s | everything else |
| `SLOW_READS_PER_CYCLE` | 6 | most module requests per DME cycle, stalest first |
| `MODULE_TIMEOUT` | 0.5 s | per module request |
| `NO_ANSWER_BACKOFF` | 30 s | a module that timed out is left alone |

Between refreshes the last values are reused, so every sample and every log
row is complete. An identifier refused with `0x11`/`0x12`/`0x31` is never
asked again in that session. The cost of watching hundreds of module values
is bounded: at most six short requests per cycle.

## GUI

```
Tk main thread                         background threads
--------------                         ------------------
App._drain() every 60 ms  <-- queue <-- Poller (owns the socket, reads, logs CSV)
                                   <-- DemoFeed / ReplayFeed (playback.py)
                                   <-- discover / scan / faults / check workers
```

Tk may only be touched from the main thread, so workers put messages on
`App.q` and `_drain()` applies them:

| Message | From | Effect |
|---|---|---|
| `sample` | Poller | table, chart, answers learned |
| `feedsample`, `feeddone` | demo / replay | same display; ignored if from a stopped feed |
| `status`, `error` | Poller | connection state; `error` means the connection is gone |
| `joberror` | fault read, check, discover | error box only: a failed job is not a lost connection |
| `discovered`, `module`, `scanline` | discover / scan | IP filled in, modules for Faults, scan window |
| `faultmsg`, `faults_done` | fault read | fault window |
| `logfile` | Poller | logging moved to `run_2.csv` after a channel change |
| `checkres`, `checkdone` | check | answers store, picker marks |

**Sharing the connection.** The car gets one connection. While connected,
anything else that needs it (fault read, check) goes through
`Poller.submit(job)`. A job that returns a generator advances one step per
poll cycle, so logging slows but never stops.

**Logging.** Written in the Poller thread; flushed and `fsync`ed about once a
second, so a crash or flat battery loses at most a second. Changing the
watched channels mid-log starts `run_2.csv` rather than a second header row.

**Demo and replay.** Until Connect, `DemoFeed` makes up plausible values on
real channels (a 30 s idle/pull/cruise/lift loop, oil warming up, laps of a
1 km circle at Circuit of the Americas). *Open log...* replays a recorded CSV
at its own pace (gaps over 2 s shortened). Both stop on Connect, and their
points are dropped so they never mix with real ones.

**Chart.** Up to six lines, each on its own scale (engine speed and latitude
can share it), from the rows selected in the table or else the first numeric
channels. Enumerated values chart as numbers.

## Picker and the answers store

The channel picker is a `ttk.Treeview` built from an index made once at
start-up (module label, function group, record, label, search text per
channel; about half a second for ~9,900 channels). Groups are inserted
eagerly; channels only when a group opens, so a rebuild takes ~10 ms. A
filter with ≤ 300 matches opens them.

The display language (English / Deutsch) is kept in
`%APPDATA%\n63read\settings.json`; switching it rebuilds the picker index and
relabels the watch table.

What the car answers is kept in `%APPDATA%\n63read\availability.json`,
keyed `0xMM:0xDDDD` → `ok` / `size` / `refused` / `no answer`:

- **live reads** mark `ok` or `refused`; saved every 10 s, with picker marks
  updated in place so a selection in progress survives;
- **Check car now** runs `core.check_steps()` (10 identifiers per poll cycle
  while connected, or on its own connection otherwise); a module with three
  unanswered identifiers in a row is skipped;
- **Import** reads a `n63read check --out` CSV.

"No answer" never overwrites a known answer, since a module may simply have
been asleep.

## Testing

`tests/fake_car.py` speaks enough HSFZ to stand in for the car: it acks
every request, answers modules it has (DME `0x12`, gateway `0x10`, BDC
`0x40`, head unit `0x63`) and stays silent elsewhere, exactly like an empty
address. Tests drive the CLI and the GUI against it. Tk roots are created
once per test session, because creating them repeatedly is flaky on some
Windows Pythons. CI runs `pytest`, builds both exes, and runs
`n63read-cli.exe selftest` to prove every data file made it into the build.
