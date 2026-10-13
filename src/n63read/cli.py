"""
n63read - read BMW N63TU2 DME measurements over ENET (HSFZ). Read-only.

Usage
-----
  # 1. Find the car on the network
  n63read discover

  # 2. Find which modules answer (this is how you locate the slave DME)
  n63read scan --ip 169.254.x.x

  # 3. Read one or more channels once
  n63read read --ip 169.254.x.x ILAG1 ILAG2

  # 4. Log channels to CSV until Ctrl-C
  n63read log --ip 169.254.x.x --interval 0.2 \\
      --out bank2_test.csv ILAG1 ILAG2 ILUZ5 ILUZ7

  # 5. Read fault memory, decoded against the SGBD text catalog
  n63read faults --ip 169.254.x.x --target 0x12

  # Target a module other than the master DME (0x12)
  n63read read --ip 169.254.x.x --target 0x13 ILAG2
"""

import argparse
import csv
import os
import sys
import time

from n63read import core
from n63read.core import (DEFAULT_CSV, DME_MASTER, FAULT_CSV, NRC, TESTER_ADDR,
                          Hsfz, HsfzError, NegativeResponse, decode)


def _catalog(path):
    try:
        return core.load_catalog(path)
    except FileNotFoundError as e:
        sys.exit(str(e))


# -------------------------------------------------------------- operations

def op_discover(args):
    if args.list_interfaces:
        print("Local IPv4 addresses (link-local first):")
        for a in core.local_ipv4s():
            print("   ", a)
        return
    hits = core.discover(args.timeout, args.bind)
    if not hits:
        print("No response. Check you're on the adapter's network and the "
              "ignition is on (position 2).")
        return
    for ip, payload in hits:
        print("%-16s %s" % (ip, payload.hex()))
        tail = payload[6:]
        printable = bytes(c if 32 <= c < 127 else 0x2E for c in tail)
        print(" " * 17 + printable.decode("ascii", "replace"))


def op_faults(args):
    """Read fault memory via 0x19 and decode it against the SGBD catalogs."""
    if args.all or args.out:
        return _faults_report(args)

    texts = core.load_fault_texts()
    lang = "text_de" if args.german else "text_en"

    with Hsfz(args.ip, source=args.source, timeout=args.timeout) as h:
        try:
            dtcs = h.read_dtcs(args.target, args.mask)
        except NegativeResponse as e:
            sys.exit("module 0x%02X refused 0x19: %s"
                     % (args.target, NRC.get(e.code, "nrc 0x%02X" % e.code)))

    if not dtcs:
        print("No fault codes reported by module 0x%02X." % args.target)
        return

    if not texts:
        print("(%s not found - showing raw codes)\n" % FAULT_CSV)

    print("%d fault code(s) from module 0x%02X:\n" % (len(dtcs), args.target))
    unknown = 0
    for code, status in sorted(dtcs):
        entry = texts.get(code.upper())
        if entry:
            desc = entry.get(lang) or entry.get("text_de") or ""
        else:
            desc = "(not in catalog)"
            unknown += 1
        print("  %-8s %s" % (code, desc))
        print("  %-8s status 0x%02X - %s" % ("", status, core.dtc_status_summary(status)))
        if args.verbose:
            print("  %-8s %s" % ("", ", ".join(core.dtc_status_flags(status)) or "no bits set"))
    if unknown:
        print("\n%d code(s) had no catalog entry. The SGBD for this module "
              "may not be among those extracted - see PROJECT.md." % unknown)


def _faults_report(args):
    """faults --all / --out: every module (or one), printed and optionally saved."""
    lang = "text_de" if args.german else "text_en"
    with Hsfz(args.ip, source=args.source, timeout=args.timeout) as h:
        if args.all:
            lo, hi = core.SWEEP_RANGE
            print("Finding modules 0x%02X-0x%02X..." % (lo, hi))
            modules = core.sweep_modules(
                h, lo, hi, progress=lambda a: print("\r  0x%02X" % a, end="", flush=True))
            print("\r%d module(s) answered.\n" % len(modules))
        else:
            modules = [args.target]
        rows = core.read_all_faults(
            h, modules, progress=lambda a: print("Reading 0x%02X..." % a, end="\r", flush=True))

    current = None
    for r in rows:
        if r["module"] != current:
            current = r["module"]
            print("\n%s  %s" % (r["module"], r["module_name"]))
        if r["code"]:
            print("  %-8s %s" % (r["code"], r[lang] or r["text_de"]))
            print("  %-8s status %s - %s" % ("", r["status"], r["state"]))
        else:
            print("  %s" % r["state"])

    faults = sum(1 for r in rows if r["code"])
    print("\n%d fault(s) across %d module(s)." % (faults, len(modules)))
    if args.out:
        core.write_fault_csv(args.out, rows)
        print("Saved to %s" % args.out)


def op_scan(args):
    """
    Sweep target addresses with TesterPresent and report which answer.

    This is how you find the slave DME. Known: 0x12 master DME, 0x40 BDC,
    0x10 gateway. An unexpected responder near 0x12 is the candidate.
    """
    lo, hi = args.range
    print("Scanning targets 0x%02X-0x%02X (ignition on, engine off)...\n" % (lo, hi))
    alive = []
    with Hsfz(args.ip, source=args.source, timeout=args.timeout) as h:
        for target in range(lo, hi + 1):
            try:
                h.tester_present(target)
            except NegativeResponse as e:
                # A negative response still proves something is listening.
                alive.append((target, "responds (%s)" % NRC.get(e.code, "nrc 0x%02X" % e.code)))
                print("  0x%02X  responds (negative: %s)" % (target, NRC.get(e.code, "0x%02X" % e.code)))
            except HsfzError:
                pass
            except Exception as e:                      # noqa: BLE001
                print("  0x%02X  error: %s" % (target, e), file=sys.stderr)
            else:
                alive.append((target, "responds"))
                print("  0x%02X  responds" % target)
    print("\n%d module(s) answered." % len(alive))


def op_read(args):
    cat = _catalog(args.csv)
    enums = core.load_enum_tables()
    lang = "text_de" if args.german else "text_en"
    with Hsfz(args.ip, source=args.source, timeout=args.timeout) as h:
        for name in args.names:
            meta = cat.get(name)
            if not meta:
                print("%-34s UNKNOWN (not in catalog)" % name)
                continue
            did = int(meta["ID"], 16)
            try:
                raw = h.read_did(args.target, did)
            except HsfzError as e:
                print("%-34s %s  <- %s" % (name, meta["ID"], e))
                continue
            value, unit = decode(raw, meta, enums, lang)
            print("%-34s %s  %-14s %-8s raw=%s   %s"
                  % (name, meta["ID"], value, unit, raw.hex(), meta["INFO"][:40]))


def op_log(args):
    cat = _catalog(args.csv)
    chans = []
    for name in args.names:
        meta = cat.get(name)
        if not meta:
            sys.exit("unknown channel: %s" % name)
        chans.append((name, int(meta["ID"], 16), meta))

    out = open(args.out, "w", newline="", encoding="utf-8")
    w = csv.writer(out)
    w.writerow(["Time"] + [n for n, _, _ in chans])
    print("Logging %d channel(s) to %s - Ctrl-C to stop" % (len(chans), args.out))

    t0 = last_flush = time.time()
    n = 0
    try:
        with Hsfz(args.ip, source=args.source, timeout=args.timeout) as h:
            while True:
                row = [round(time.time() - t0, 3)]
                for _, did, meta in chans:
                    try:
                        value, _ = decode(h.read_did(args.target, did), meta)
                    except Exception:                   # noqa: BLE001
                        value = ""
                    row.append(value)
                w.writerow(row)
                n += 1
                # Push to disk about once a second; a crash loses at most that.
                if time.time() - last_flush >= 1.0:
                    out.flush()
                    os.fsync(out.fileno())
                    last_flush = time.time()
                    print("\r%d samples, %.1f s" % (n, time.time() - t0), end="")
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        out.close()
        print("wrote %d samples to %s" % (n, args.out))


# --------------------------------------------------------------------- CLI

def hexint(s):
    return int(s, 0)


def build_parser():
    p = argparse.ArgumentParser(prog="n63read", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, need_ip=True):
        if need_ip:
            sp.add_argument("--ip", required=True, help="ECU/gateway IP (see `discover`)")
        sp.add_argument("--source", type=hexint, default=TESTER_ADDR,
                        help="tester address (default 0xF4)")
        sp.add_argument("--timeout", type=float, default=2.0)
        sp.add_argument("--csv", default=DEFAULT_CSV,
                        help="measurement catalog CSV")

    sp = sub.add_parser("discover", help="find the car via UDP broadcast")
    sp.add_argument("--timeout", type=float, default=3.0)
    sp.add_argument("--bind", help="local IP of the ENET adapter (169.254.x.x)")
    sp.add_argument("--list-interfaces", action="store_true",
                    help="show local IPv4 addresses and exit")
    sp.set_defaults(func=op_discover)

    sp = sub.add_parser("scan", help="sweep target addresses to find modules")
    common(sp)
    sp.add_argument("--range", type=hexint, nargs=2, default=[0x00, 0x60],
                    metavar=("LO", "HI"))
    sp.set_defaults(func=op_scan)

    sp = sub.add_parser("read", help="read channels once")
    common(sp)
    sp.add_argument("--target", type=hexint, default=DME_MASTER)
    sp.add_argument("--german", action="store_true",
                    help="show BMW's German text for enumerated values")
    sp.add_argument("names", nargs="+", help="ARG names, e.g. ILAG1 ILAG2")
    sp.set_defaults(func=op_read)

    sp = sub.add_parser("faults", help="read and decode fault memory (0x19)")
    common(sp)
    sp.add_argument("--target", type=hexint, default=DME_MASTER)
    sp.add_argument("--mask", type=hexint, default=0xFF,
                    help="statusOfDTC mask (default 0xFF = all stored DTCs)")
    sp.add_argument("--german", action="store_true",
                    help="show BMW's German text instead of the translation")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="also list the individual statusOfDTC bits")
    sp.add_argument("--all", action="store_true",
                    help="find every module that answers and read them all")
    sp.add_argument("--out", metavar="CSV",
                    help="also save the faults to a CSV file")
    sp.set_defaults(func=op_faults)

    sp = sub.add_parser("log", help="poll channels into a CSV")
    common(sp)
    sp.add_argument("--target", type=hexint, default=DME_MASTER)
    sp.add_argument("--interval", type=float, default=0.2)
    sp.add_argument("--out", default="n63log.csv")
    sp.add_argument("names", nargs="+")
    sp.set_defaults(func=op_log)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ConnectionError, TimeoutError, HsfzError) as e:
        # No car, wrong IP, or firewall: a message, not a traceback.
        sys.exit("cannot talk to %s: %s" % (getattr(args, "ip", "the car"), e))


if __name__ == "__main__":
    main()
