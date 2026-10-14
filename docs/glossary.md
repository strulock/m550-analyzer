# Glossary

Terms in channel names, SGBD files and these docs. Channel names and
descriptions come from BMW in German; the English is a glossary translation
(see [Data sources](data-sources.md#translation)), so the German words below
show up a lot.

## Modules and tools

| Term | Meaning |
|---|---|
| **ACSM** | Advanced Crash Safety Module: airbag controller (`0x01`); has an IMU |
| **ARS** | Aktive Roll-Stabilisierung: active anti-roll bars |
| **BDC** | Body Domain Controller (`0x40`): lights, windows, wipers, locks, fuel level, gateway functions |
| **CID** | Central Information Display: the head unit's screen |
| **DDE** | Digitale Diesel-Elektronik: diesel engine computer (not this car) |
| **DME** | Digitale Motor-Elektronik: petrol engine computer (`0x12` master) |
| **DSC** | Dynamic Stability Control (`0x29`): ABS, traction, tire pressure (RDC) |
| **EDIABAS** | BMW's diagnostic runtime that executes SGBD jobs; under ISTA, E-Sys, INPA |
| **EGS** | Elektronische Getriebesteuerung: transmission controller (`0x18`) |
| **EHC** | Electronic Height Control: self-levelling air suspension |
| **EKP / EKPM** | Elektrische Kraftstoffpumpe (Modul): fuel pump control (`0x17`) |
| **ENET** | Ethernet on the OBD port |
| **EPS** | Electric Power Steering (`0x30`) |
| **E-Sys / ISTA / INPA** | BMW coding / workshop / engineering tools built on EDIABAS |
| **HU** | Head unit; here NBT Evo (`0x63`) |
| **IHKA** | Integrierte Heiz-Klima-Automatik: automatic climate control (`0x78`) |
| **IMU** | Inertial measurement unit: accelerometers and gyros (in the ACSM) |
| **KOMBI** | Instrument cluster (`0x60`) |
| **LMV** | Längsmomentverteiler: xDrive transfer case (`0x19`) |
| **NBT Evo** | "Next Big Thing Evolution": the iDrive head unit generation in this car |
| **ENAVEVO** | Entry Nav Evolution: a cheaper head unit, listed at `0x63` in the G30 map; not this car's |
| **PMA** | Parkmanöverassistent: park assist |
| **RDC** | Reifendruck-Control: tire pressure monitoring (in the DSC) |
| **SGBD** | Steuergeräte-Beschreibungsdatei: a module's diagnostic description (`.prg`) |
| **VDP** | Vertikaldynamik-Plattform: suspension / damping controller (`0x76`) |
| **ZGW** | Zentrales Gateway: central gateway (`0x10`) |

## Protocol and data

| Term | Meaning |
|---|---|
| **HSFZ** | BMW's diagnostics over TCP/IP, port 6801; see [Protocol](protocol.md) |
| **UDS** | Unified Diagnostic Services (ISO 14229): the request/response language |
| **DID** | Data identifier, the 16-bit number read with service `0x22` (`ID` column) |
| **NRC** | Negative response code (`7F <service> <code>`) |
| **DTC** | Diagnostic trouble code (fault code), 3 bytes / 6 hex digits |
| **statusOfDTC** | the status byte after each DTC: present, pending, stored... |
| **SVK** | Steuergeräte-Verbau-Kennung: a module's software/hardware part list (`22 F1 01`) |
| **SGBM** | Software unit identifier (process class + ID + version) inside the SVK |
| **SGBD index** | 3-byte number (`22 F1 50`) naming a module's SGBD variant |
| **SP2018 / I-Stufe** | BMW software level (integration level); variants differ by it |
| **35up** | BMW's G-series electrical architecture of the G11/G30 family |
| **Record** | one identifier's reply carrying several values (a `RES_` table) |
| **ARG** | channel / job name column |
| **RESULTNAME** | a value's name (`STAT_..._WERT`) |
| **DATENTYP** | data type: `unsigned int`, `data[5]`, `string` |
| **EINHEIT** | unit; `0-n` = enumerated, `HEX`, `TEXT` |
| **MUL / DIV / ADD** | scaling: value = raw × MUL ÷ DIV + ADD |
| **L/H** | byte order; `high` = most significant byte first |
| **NAME** | the value table of an enumerated value |
| **RES_TABELLE** | the record table listing a reply's fields |
| **FORTTEXTE** | fault code → text table |
| **SG_FUNKTIONEN** | an SGBD's table of readable/writable identifiers |
| **STAT_ / _WERT / _TEXT** | result name prefix / "value" / "text" suffixes |
| **Koppelgröße** | value exchanged between master and slave DME over CAN |

## German in channel names

| German | English | | German | English |
|---|---|---|---|---|
| Wert | value | | Istwert / Sollwert | actual / target value |
| Zähler, Zaehler | counter | | Status, Zustand | state |
| Spannung | voltage | | Strom | current |
| Temperatur | temperature | | Druck | pressure |
| Drehzahl | rotational speed, rpm | | Geschwindigkeit | speed (road) |
| Gang, Istgang | gear, current gear | | Fahrstufe | P/R/N/D/M |
| Wandler(kupplung) | torque converter (lock-up) | | Kupplung | clutch |
| Getriebe | transmission | | Wählhebel | gear selector |
| Laufunruhe | uneven running (misfire indicator) | | Aussetzer | misfire |
| Zylinder (ZYL) | cylinder | | Bank | cylinder bank |
| Zündung | ignition | | Klopfen | knock |
| Ladedruck | boost pressure | | Drosselklappe (DK) | throttle |
| Kraftstoff | fuel | | Tank, Tankgeber | tank, tank level sender |
| Öl, Oel | oil | | Kühlmittel | coolant |
| Lüfter | fan | | Pumpe | pump |
| Reifen | tire | | Rad | wheel |
| Bremse, Brems- | brake | | Bremsbelag | brake pad |
| Lenkung, Lenkwinkel | steering, steering angle | | Lenkmoment | steering torque |
| Ritzel | pinion | | Zahnstange | (steering) rack |
| Höhenstand | ride height | | Dämpfer | damper |
| Beschleunigung | acceleration | | Moment | torque |
| Licht, Leuchte | light, lamp | | Spiegel | mirror |
| Fensterheber (FH) | window lifter | | Wischer | wiper |
| Sitz | seat | | Tür | door |
| Klima, Kältemittel | climate, refrigerant | | Verdichter, Kompressor | compressor |
| Breitengrad / Längengrad | latitude / longitude | | Höhe | height |
| Gültigkeit | validity | | Satellit | satellite |
| Lesen / Steuern | read / control | | Adaption | adaptation (learned value) |
| Klemme (KL) 30 / 15 | terminal 30 (battery) / 15 (ignition) | | PWF | vehicle power state |
| FES | drive mode switch (Comfort/Sport) | | CBS | condition-based service |

## Positions

| Abbreviation | Meaning |
|---|---|
| VL / VR / HL / HR | front left / front right / rear left / rear right (vorne/hinten, links/rechts) |
| VA / HA | front / rear axle |
| FA / BF | driver (Fahrer) / front passenger (Beifahrer) |
| FAH / BFH | rear behind driver / rear behind passenger |
| A / B / C | (fuel pump) motor phases |
