# n63read documentation

Reference for how this tool talks to a 2018 BMW M550i xDrive (G30, N63TU2)
over ENET, where its data comes from, and how every value is decoded. The
top-level [README](../README.md) covers installing and using the app;
[PROJECT.md](../PROJECT.md) is the original design brief and safety rules.

| Page | What's in it |
|---|---|
| [Protocol](protocol.md) | HSFZ over Ethernet: discovery, framing, addressing, the read-only services, negative responses, timeouts |
| [Modules](modules.md) | Every module the app reads, its address and SGBD file, which files fit this car and which were rejected, and why |
| [SGBD format](sgbd-format.md) | Inside a BMW `.prg` file: obfuscation, the table directory, `SG_FUNKTIONEN`, result and value tables, fault texts, and decoding hand-coded jobs from their bytecode |
| [Decoding](decoding.md) | Raw bytes to values: data types, scaling, byte order, value tables, multi-value records, explicit positions, GPS coordinates, function groups |
| [Data sources](data-sources.md) | Where each data file comes from, how to regenerate it, and the translation pipeline |
| [Architecture](architecture.md) | How the code is organised: the reader and its refresh schedule, the GUI's threads and messages, the answers store, the check |
| [In-car checklist](in-car-checklist.md) | What is verified on the car and what isn't, and the session that settles it |
| [Glossary](glossary.md) | BMW and German terms used in channel names, files and this documentation |
| [Channel lookup](channels/README.md) | Every one of the ~9,900 channels: identifier, field, type, unit, scaling, value table, description. Generated from the catalogs |

## The short version

- The car's diagnostic port speaks **HSFZ** (BMW's diagnostics over TCP/IP)
  on port 6801. The tool only ever **reads**: UDS services `0x22`, `0x19`
  and `0x3E`.
- Each module (engine, transmission, DSC, head unit...) answers at its own
  1-byte address. What it can report, and how to decode it, is defined in
  BMW's **SGBD** file for that module (`.prg`).
- `tools/module_catalog.py` turns an SGBD into a **catalog** CSV, one row per
  value. The app reads catalogs, never `.prg` files, and the `.prg` files are
  not in this repo.
- A catalog only helps if it's the right file for the car's module. BMW ships
  many variants, so `n63read check` (or *Answers > Check car now* in the GUI)
  compares the car's replies with the catalog before you trust it.
