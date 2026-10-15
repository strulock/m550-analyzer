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

  # 6. Check every module's catalog against the car (which files fit)
  n63read check --ip 169.254.x.x --out check.csv

  # 7. GPS position from the head unit (0x63); --watch to keep reading
  n63read gps --ip 169.254.x.x --watch

  # Target a module other than the master DME (0x12)
  n63read read --ip 169.254.x.x --target 0x13 ILAG2
"""

import argparse
import csv
import os
import sys
import time

from n63read import __version__, core, diag, share, update
from n63read.core import (DEFAULT_CSV, DME_MASTER, FAULT_CSV, NRC, TESTER_ADDR,
                          HsfzError, NegativeResponse, decode)


def _catalog(path):
    """DME channels from `path`, plus the other modules' (HU_, EGS_, DSC_...)."""
    try:
        cat = core.load_catalog(path)
    except FileNotFoundError as e:
        sys.exit(str(e))
    cat.update(core.load_module_catalogs())
    return core.add_bank2(cat)


def _tester(args, timeout=None):
    """The car: shared from the app or a running log if one holds it, else
    this command's own connections."""
    return share.open_tester(args.ip, args.source,
                             args.timeout if timeout is None else timeout)


# -------------------------------------------------------------- operations

def op_discover(args):
    if args.list_interfaces:
        print("Local IPv4 addresses (link-local first):")
        for a in core.local_ipv4s():
            print("   ", a)
        return
    try:
        hits = core.discover(args.timeout, args.bind)
    except core.AddressNotAvailable as e:
        link = core.link_local(core.local_ipv4s())
        sys.exit("discover: %s\n%s" % (e, "ENET addresses on this PC now: " + ", ".join(link)
                                        if link else "This PC has no 169.254.x.x "
                                        "(ENET) address right now."))
    if not hits:
        print("No response. Check you're on the adapter's network and the "
              "ignition is on (position 2).")
        near = core.link_neighbours(args.bind) if args.bind else []
        if near:
            print("But these are on the car's link (probably the car): %s\n"
                  "Try: n63read read --ip %s ILAG1" % (", ".join(near), near[0]))
        return
    for ip, payload in hits:
        print("%-16s %-5s %s" % (ip, core.reply_protocol(payload), payload.hex()))
        tail = payload[6:]
        printable = bytes(c if 32 <= c < 127 else 0x2E for c in tail)
        print(" " * 17 + printable.decode("ascii", "replace"))


def op_faults(args):
    """Read fault memory via 0x19 and decode it against the SGBD catalogs."""
    if args.all or args.out:
        return _faults_report(args)

    texts = core.load_fault_texts()
    lang = "text_de" if args.german else "text_en"

    with _tester(args) as h:
        try:
            dtcs = h.read_dtcs(args.target, args.mask)
        except NegativeResponse as e:
            sys.exit("module 0x%02X refused 0x19: %s"
                     % (args.target, NRC.get(e.code, "nrc 0x%02X" % e.code)))

    hidden = 0
    if not args.every_code:
        kept = [d for d in dtcs if core.dtc_failed(d[1])]
        hidden, dtcs = len(dtcs) - len(kept), kept
    if not dtcs:
        print("No fault codes reported by module 0x%02X%s." % (
            args.target, " (%d listed codes never failed; --every-code shows them)"
            % hidden if hidden else ""))
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
    if hidden:
        print("\n%d listed code(s) never failed; --every-code shows them." % hidden)
    if unknown:
        print("\n%d code(s) had no catalog entry. The SGBD for this module "
              "may not be among those extracted - see PROJECT.md." % unknown)


def _faults_report(args):
    """faults --all / --out: every module (or one), printed and optionally saved."""
    lang = "text_de" if args.german else "text_en"
    with _tester(args) as h:
        if args.all:
            lo, hi = core.SWEEP_RANGE
            print("Finding modules 0x%02X-0x%02X, waiting %g s per address..."
                  % (lo, hi, args.probe_timeout))
            modules = core.sweep_modules(
                h, lo, hi, progress=lambda a: print("\r  0x%02X" % a, end="", flush=True),
                probe_timeout=args.probe_timeout)
            print("\r%d module(s) answered.\n" % len(modules))
        else:
            modules = [args.target]
        rows = core.read_all_faults(
            h, modules, progress=lambda a: print("Reading 0x%02X..." % a, end="\r", flush=True),
            everything=args.every_code)

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
    print("Scanning targets 0x%02X-0x%02X, waiting %g s per address "
          "(ignition on, engine off)...\n" % (lo, hi, args.probe_timeout))
    alive = []
    # The per-address wait is the probe timeout, not --timeout: most
    # addresses are empty and each one costs the full wait.
    with _tester(args, args.probe_timeout) as h:
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
    layouts = core.record_layouts(cat)
    enums = core.load_enum_tables()
    lang = "text_de" if args.german else "text_en"
    with _tester(args) as h:
        for name in args.names:
            meta = cat.get(name)
            if not meta:
                print("%-34s UNKNOWN (not in catalog)" % name)
                continue
            did = int(meta["ID"], 16)
            module = core.channel_target(meta, args.target)
            try:
                raw = h.read_did(module, did)
            except HsfzError as e:
                print("%-34s %s  <- %s" % (name, meta["ID"], e))
                continue
            if meta.get("MODULE"):
                # Another module: the reply is a record; this channel is one field.
                value, unit, note = core.decode_record(
                    raw, layouts[(module, did)])[name]
                value = core.enum_text(value, meta, enums, lang) or (
                    note if value is None else value)
            else:
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

    if not args.out:
        args.out = os.path.join(core.logs_dir(), time.strftime("n63_%Y-%m-%d_%H-%M-%S.csv"))
    out = open(args.out, "w", newline="", encoding="utf-8")
    w = csv.writer(out)
    w.writerow(["Time"] + [n for n, _, _ in chans])
    print("Logging %d channel(s) to %s - Ctrl-C to stop" % (len(chans), args.out))

    t0 = last_flush = time.time()
    n = 0
    host = None
    try:
        reader = core.ChannelReader(core.record_layouts(cat), args.target)
        with _tester(args) as h:
            if not getattr(h, "shared", False):
                # We hold the car: lend it to other n63read processes.
                host = share.Host(args.ip).start()
            while True:
                if host:
                    host.serve(h)
                    h.tidy()
                row = [round(time.time() - t0, 3)]
                sample = reader.read(h, chans)
                for name, _, _ in chans:
                    value = sample[name][0]
                    row.append("" if value is None else value)
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
        if host:
            host.stop()
        out.close()
        print("wrote %d samples to %s" % (n, args.out))


def op_update(args):
    """Check GitHub for a newer release and, unless --check, install it."""
    try:
        if args.check or not update.installed():
            info = update.check()
            if info["newer"]:
                print("n63read %s is available (this is %s): %s"
                      % (info["latest"], info["current"], info["url"]))
            else:
                print("n63read %s is the latest version." % info["current"])
            if info["newer"] and not args.check:
                print("Running from source: update with git, not here."
                      if not update.running_exe() else
                      "This copy wasn't installed by %s; download that to update." % update.SETUP)
            return
        info = update.update(progress=lambda got, size: print(
            "\r  %d / %d KB" % (got // 1024, size // 1024), end="", flush=True))
        if info["setup"]:
            update.install(info["setup"])
    except update.UpdateError as e:
        diag.record("update", state="failed", error=str(e))
        sys.exit("update failed: %s" % e)
    diag.record("update", state="installing" if info["setup"] else "checked",
                current=info["current"], latest=info["latest"])
    if info["setup"]:
        print("\nInstalling n63read %s in the background; close any open BMW Analyzer "
              "windows. It takes under a minute." % info["latest"])
    else:
        print("n63read %s is the latest version." % info["current"])


def op_diag(args):
    """Connection check, step by step, recorded for later analysis."""
    if args.report:
        n = diag.report(args.report, days=args.days)
        print("Saved %d journal entries from the last %d days to %s" % (n, args.days, args.report))
        print("Packet captures, if any, are .pcapng files in %s" % diag.journal_dir())
        return
    capture = None
    if args.capture:
        capture = diag.Capture(args.capture)
        try:
            capture.start()
            print("Capturing all packets on this PC for up to %d s..." % args.capture)
        except Exception as e:                                      # noqa: BLE001
            print("No packet capture: %s" % e)
            capture = None
    try:
        result = diag.full_check(args.bind, args.ip, args.timeout)
    finally:
        if capture:
            saved = capture.stop()
            print("Packet capture: %s" % (saved or "failed (see the journal)"))
    if not result["ok"]:
        sys.exit(1)


def op_selftest(args):
    """Check every data file is bundled and loads; exit non-zero if not.

    A missing module catalog is not an error at runtime (that module's
    channels just don't appear), so a build that forgot one would pass
    unnoticed. CI runs this against the built exe.
    """
    names = list(core.DATA_FILES)
    for name in core.MODULE_CATALOGS:
        names += [name + "_measurements.csv", name + "_enum_tables.csv"]
    problems = []
    for name in names:
        path = core.resource_path(name)
        if not os.path.exists(path):
            problems.append("%s: not found" % name)
            print("  %-42s MISSING" % name)
            continue
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = sum(1 for _ in csv.reader(f)) - 1
        print("  %-42s %6d rows" % (name, rows))
        # A module may have no enum tables (the fuel pump has none), so
        # those only have to exist; everything else must have rows.
        if rows <= 0 and not name.endswith("_enum_tables.csv"):
            problems.append("%s: no rows" % name)
    for name in core.ICON_FILES:
        found = os.path.exists(core.resource_path(name))
        print("  %-42s %s" % (name, "ok" if found else "MISSING"))
        if not found:
            problems.append("%s: not found" % name)
    loaded = {
        "DME channels": len(core.load_catalog()),
        "module channels": len(core.load_module_catalogs()),
        "enum tables": len(core.load_enum_tables()),
        "fault texts": len(core.load_fault_texts()),
        "status texts": len(core.load_status_texts()),
    }
    for what, n in loaded.items():
        print("  %-42s %6d" % (what, n))
        if not n:
            problems.append("%s: none loaded" % what)
    if problems:
        sys.exit("selftest failed:\n  " + "\n  ".join(problems))
    print("selftest ok")


def op_check(args):
    """Read every catalogued identifier and check its reply size against the file.

    This is how to tell whether an SGBD-derived catalog matches the car: a
    module whose replies are nearly all the predicted size has the right
    file. Read-only (0x22). Raw bytes go to --out, except for private values.
    """
    cat = _catalog(args.csv)
    layouts = core.check_layouts(cat, args.target)
    modules = sorted({k[0] for k in layouts}) if not args.module else args.module
    out = None
    if args.out:
        out = open(args.out, "w", newline="", encoding="utf-8")
        w = csv.writer(out)
        w.writerow(["module", "did", "job", "status", "detail", "reply_bytes", "raw"])
    try:
        with _tester(args) as h:
            for module in modules:
                keys = sorted(k for k in layouts if k[0] == module)
                name = core.MODULE_LABELS.get(module, "DME" if module == args.target
                                              else "0x%02X" % module)
                print("\n%s (0x%02X): %d identifiers" % (name, module, len(keys)))
                counts, silent = {}, 0
                for i, key in enumerate(keys, 1):
                    fields = layouts[key]
                    status, detail, raw = core.check_identifier(h, key, fields)
                    counts[status] = counts.get(status, 0) + 1
                    silent = silent + 1 if status == "no answer" else 0
                    job = fields[0].get("JOB") or fields[0]["ARG"]
                    if args.verbose or status == "size":
                        print("  0x%04X %-40s %-9s %s" % (key[1], job[:40], status, detail))
                    else:
                        print("\r  %d/%d" % (i, len(keys)), end="", flush=True)
                    if out:
                        private = any(core.is_sensitive(f["ARG"]) for f in fields)
                        w.writerow(["0x%02X" % module, "0x%04X" % key[1], job, status,
                                    detail, len(raw), "(private)" if private else raw.hex()])
                    if silent >= 3:
                        print("\n  three identifiers in a row went unanswered; skipping the rest")
                        counts["no answer"] += len(keys) - i
                        break
                print("\r  " + ", ".join("%s %d" % kv for kv in sorted(counts.items())))
                print("  -> " + core.check_verdict(counts))
    finally:
        if out:
            out.close()
            print("\nSaved to %s" % args.out)


def op_gps(args):
    """Read the head unit's GPS fix and position. Raw bytes shown for checking."""
    with _tester(args) as h:
        while True:
            try:
                status = h.read_did(args.target, core.GPS_STATUS_DID)
                fix = core.GPS_FIX.get(status[0], "0x%02X" % status[0]) if status else "?"
                raw = h.read_did(args.target, core.GPS_POSITION_DID)
            except NegativeResponse as e:
                sys.exit("module 0x%02X refused: %s"
                         % (args.target, NRC.get(e.code, "nrc 0x%02X" % e.code)))
            try:
                p = core.parse_gps_position(raw)
                print("%-24s %s  lat %.6f  lon %.6f  height %d   raw=%s"
                      % (fix, "valid  " if p["valid"] else "INVALID", p["lat"], p["lon"],
                         p["height"], raw.hex()))
            except ValueError as e:
                print("%-24s %s   raw=%s" % (fix, e, raw.hex()))
            if not args.watch:
                return
            time.sleep(args.interval)


# --------------------------------------------------------------------- CLI

def op_mcp(args):
    """Serve Claude over MCP, or set Claude up to use it."""
    from n63read import mcp
    mcp.main(args)


def hexint(s):
    return int(s, 0)


def probe_seconds(s):
    try:
        return core.parse_probe_timeout(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def probe_arg(sp):
    sp.add_argument("--probe-timeout", type=probe_seconds, default=core.PROBE_TIMEOUT,
                    metavar="SECONDS",
                    help="wait at each address when looking for modules (default "
                         "%g; raise it if modules are missed)" % core.PROBE_TIMEOUT)


def build_parser():
    p = argparse.ArgumentParser(prog="n63read", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="n63read %s" % __version__)
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
    sp.add_argument("--range", type=hexint, nargs=2, default=list(core.SWEEP_RANGE),
                    metavar=("LO", "HI"))
    probe_arg(sp)
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
    sp.add_argument("--every-code", action="store_true",
                    help="also list codes that never failed (modules list every "
                         "code they can raise)")
    probe_arg(sp)
    sp.add_argument("--out", metavar="CSV",
                    help="also save the faults to a CSV file")
    sp.set_defaults(func=op_faults)

    sp = sub.add_parser("log", help="poll channels into a CSV")
    common(sp)
    sp.add_argument("--target", type=hexint, default=DME_MASTER)
    sp.add_argument("--interval", type=float, default=0.2)
    sp.add_argument("--out", help="CSV to write (default: a dated file in "
                                  "Documents\\%s)" % core.LOGS_FOLDER)
    sp.add_argument("names", nargs="+")
    sp.set_defaults(func=op_log)

    sp = sub.add_parser("update", help="install the latest GitHub release")
    sp.add_argument("--check", action="store_true", help="only say whether one is available")
    sp.set_defaults(func=op_update)

    sp = sub.add_parser("diag", help="check the connection to the car step by step "
                                     "and record it (no IP needed)")
    sp.add_argument("--bind", help="this PC's ENET address (default: its 169.254.x.x)")
    sp.add_argument("--ip", help="also try this address for the car")
    sp.add_argument("--timeout", type=float, default=3.0, help="Discover wait, s")
    sp.add_argument("--capture", type=int, nargs="?", const=60, metavar="SECONDS",
                    help="also record the packets with pktmon (administrator)")
    sp.add_argument("--report", metavar="FILE",
                    help="instead: save the recent journal to FILE to send on")
    sp.add_argument("--days", type=int, default=7, help="with --report: days to include")
    sp.set_defaults(func=op_diag)

    sp = sub.add_parser("selftest", help="check every data file is bundled and loads "
                                         "(no car needed)")
    sp.set_defaults(func=op_selftest)

    sp = sub.add_parser("check", help="check each module's catalog against the car "
                                      "(reply sizes)")
    common(sp)
    sp.add_argument("--target", type=hexint, default=DME_MASTER,
                    help="DME address for the DME catalog (default 0x12)")
    sp.add_argument("--module", type=hexint, action="append",
                    help="only this module address; repeatable (default: all)")
    sp.add_argument("--out", metavar="CSV", help="save every reply to a CSV")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="print every identifier, not just size mismatches")
    sp.set_defaults(func=op_check)

    sp = sub.add_parser("mcp", help="let Claude use n63read (an MCP server on stdin/stdout)")
    sp.add_argument("--register", action="store_true",
                    help="instead: add it to Claude Code and Claude Desktop")
    sp.add_argument("--unregister", action="store_true", help="instead: remove it from them")
    sp.set_defaults(func=op_mcp)

    sp = sub.add_parser("gps", help="read GPS position from the head unit (NBT Evo)")
    common(sp)
    sp.add_argument("--target", type=hexint, default=core.HEAD_UNIT)
    sp.add_argument("--watch", action="store_true", help="keep reading until Ctrl-C")
    sp.add_argument("--interval", type=float, default=1.0)
    sp.set_defaults(func=op_gps)
    return p


def main(argv=None):
    update.tidy()
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ConnectionError, TimeoutError, HsfzError) as e:
        # No car, wrong IP, or firewall: a message, not a traceback.
        why = core.connection_problem(e)
        sys.exit("cannot talk to %s: %s%s" % (getattr(args, "ip", "the car"), e,
                                              "\n" + why if why else ""))


if __name__ == "__main__":
    main()
