# Firmware

C++ for the always-plugged-in drive logger (ESP32-S3 + W5500). It ports the
part of n63read a logger needs; nothing here is part of the Python package or
the Windows build.

| Path | What |
| --- | --- |
| `lib/hsfz/` | HSFZ client, discovery parsing, decoder. No Arduino code; same rules as `src/n63read/core.py` (`Hsfz`, `Tester`, `decode`, `split_reply`) |
| `test/host_test.cpp` | Runs `lib/hsfz` against a scripted fake gateway on a PC (CI job `firmware`) |
| `tools/bench_car.py` | Fake ENET gateway for bench tests over a real cable (discovery + reads, both DMEs) |
| `spike/` | Phase 0 bench tool: can a W5500 on an ESP32-S3 talk HSFZ to both DMEs, and how fast |
| `logger/` | The real logger firmware, later |

Read-only, as n63read: the client refuses anything but UDS 0x22 and 0x3E, and
the host test checks that nothing else is ever sent.

When `core.py` changes how it frames, routes or decodes, change `lib/hsfz` in
the same PR.

## Host test

    cd firmware
    g++ -std=c++17 -Wall -Ilib/hsfz test/host_test.cpp -o host_test && ./host_test

One function per case; `./host_test busy_retry` runs just that one.

## Spike

Board: Waveshare ESP32-S3-ETH.

1. VS Code + PlatformIO extension. Open **`firmware/spike`** (the folder with
   `platformio.ini`), not the repo root.
2. Plug the board in by USB-C and Upload. The first build downloads
   Arduino-ESP32 3.3 through the pioarduino platform. If no port shows up:
   hold BOOT, tap RESET, release BOOT, upload again.
3. Serial monitor at 115200. With no cable: `[eth] W5500 up`, then `no link`
   after 15 s.

### On the bench first

With native USB the boot messages often scroll past before the monitor opens:
press `s` for status or `a` to run everything again.

1. **Board alive**: `s` shows the MAC, our address 169.254.100.100 and
   `link down`. That proves the W5500 answers over SPI.
2. **Link**: cable from the board to a home switch or router. Expect
   `[eth] LINK UP 100 Mbit/s full duplex`; unplug for `link down`. Discovery
   finds nothing on a home network, as expected. A switch fixes up crossed
   pairs on its side, so this doesn't test the car's wiring.
3. **Dry run against a bench car**: cable the board straight to the PC's
   Ethernet port (or a USB Ethernet dongle), no router. Run
   `python firmware/tools/bench_car.py` and allow Python through the Windows
   firewall on Public networks (a direct cable counts as public). Wait about
   a minute for Windows to give the port a 169.254.x.y address, then press
   `a`. Expect discovery with VIN `WBAFAKEBENCH00001`, all eight channels
   from 0x12 and 0x13 (about 745 rpm swinging, 90 / 91.5 C coolant, 12.6 V),
   then a batch rate. The bench car prints each connection and flags any
   request other than a read.

The bench rate is not the car's: the car's DMEs take a few milliseconds per
answer, the bench car none.

### In the car

Ignition on (engine off is fine), close n63read, ENET cable from
OBD to the board's RJ45, laptop on USB-C, press RESET. A pass, in order:

1. `[eth] LINK UP 100 Mbit/s full duplex`
2. `[disc] HSFZ reply from 169.254.x.y ... VIN ...`
3. 0x12 then 0x13: tester present, then 8 channels with value, raw hex and ms
4. `[batch] ... cycles/s (n63read: 6.2)`

Keys: `a` run all, `s` status, `d` discover, `r` read once, `b` batch test, `x` close connections,
`h` help. Type `x` before unplugging, and unplug the cable when done: it holds
pin 8 high and keeps the gateway awake. Then read the same channels with
n63read to compare (the spike uses both of the car's tester connections, so
the two can't run at once).

Set `CAR_IP_OVERRIDE` in `spike/src/main.cpp` to skip discovery if the
broadcast fails but n63read shows the car's address.

| Symptom | Meaning |
| --- | --- |
| No link in 15 s | Pair mapping or the W5500's lack of auto-MDIX. Check the laptop gets a link on the same cable |
| Link, no discovery reply | Broadcast not leaving, or ignition off. Try `CAR_IP_OVERRIDE` |
| `gateway refused 0x0040` | The car still holds another tester session: wait a few minutes |
| 0x12 works, 0x13 connect fails | Another tool holds the second connection (the car allows two) |
| Values differ from n63read | Compare the raw hex; the channel rows come from `N63TU2_DME_readable_measurements.csv` |
