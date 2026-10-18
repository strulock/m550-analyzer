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
import queue
import sys
import time

from n63read import __version__, core, diag, engine, share, update
from n63read.core import (DEFAULT_CSV, DME_MASTER, FAULT_CSV, NRC, TESTER_ADDR,
                          HsfzError, NegativeResponse, decode)


def _catalog(path):
    """DME channels from `path`, plus the other modules' (HU_, EGS_, DSC_...)."""
    try:
        cat = core.load_catalog(path)
    except FileNotFoundError as e:
        sys.exit(str(e))
    cat.update(core.load_module_catalogs())
    return core.add_derived(core.add_bank2(cat))


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
        protocol = core.reply_protocol(payload)
        vin = core.reply_vin(payload)
        print("%-16s %-7s %s%s" % (ip, protocol, payload.hex(), "  VIN " + vin if vin else ""))
        tail = payload[6:]
        printable = bytes(c if 32 <= c < 127 else 0x2E for c in tail)
        print(" " * 17 + printable.decode("ascii", "replace"))
    protocols = {core.reply_protocol(p) for _, p in hits}
    if "HSFZ" not in protocols and "DoIP" in protocols:
        ip = next(ip for ip, p in hits if core.reply_protocol(p) == "DoIP")
        print("\n" + core.doip_help(ip) + "\nSave one with: n63read diag --report report.jsonl")


def op_faults(args):
    """Read fault memory via 0x19 and decode it against the SGBD catalogs."""
    if args.all or args.out:
        return _faults_report(args)

    texts = core.load_fault_texts()
    lang = "text_de" if args.german else "text_en"

    details = {}
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
        if args.details:
            fields = core.load_freeze_fields()
            details = {code: core.fault_details(h, args.target, code, fields, lang)
                       for code, _ in dtcs}
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
        for line in (core.format_details(details[code]) if code in details else []):
            print("  %-8s %s" % ("", line))
    if hidden:
        print("\n%d listed code(s) never failed; --every-code shows them." % hidden)
    if unknown:
        print("\n%d code(s) had no catalog entry. The SGBD for this module "
              "may not be among those extracted - see docs/modules.md." % unknown)


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
        details = {}
        if args.details:
            fields = core.load_freeze_fields()
            for r in rows:
                if r["code"]:
                    details[(r["module"], r["code"])] = core.fault_details(
                        h, int(r["module"], 16), r["code"], fields, lang)

    current = None
    for r in rows:
        if r["module"] != current:
            current = r["module"]
            print("\n%s  %s" % (r["module"], r["module_name"]))
        if r["code"]:
            print("  %-8s %s" % (r["code"], r[lang] or r["text_de"]))
            print("  %-8s status %s - %s" % ("", r["status"], r["state"]))
            if (r["module"], r["code"]) in details:
                for line in core.format_details(details[(r["module"], r["code"])]):
                    print("  %-8s %s" % ("", line))
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
        for target, note in core.scan(h, lo, hi, args.probe_timeout):
            if note.startswith("error"):
                print("  0x%02X  %s" % (target, note), file=sys.stderr)
                continue
            # A negative response still proves something is listening.
            alive.append(target)
            print("  0x%02X  responds%s" % (target, " (negative: %s)" % note if note else ""))
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
    names = list(args.names)
    if args.template:
        saved = dict(core.BUILTIN_TEMPLATES, **core.load_templates())
        if args.template not in saved:
            sys.exit("no template %r; available: %s" % (
                args.template, ", ".join(sorted(saved, key=str.lower))))
        names = saved[args.template] + [n for n in names if n not in saved[args.template]]
    if not names:
        sys.exit("name some channels, or --template")
    chans = []
    for name in core.with_inputs(names):     # a calculated channel needs its inputs
        meta = cat.get(name)
        if not meta:
            sys.exit("unknown channel: %s" % name)
        chans.append((name, int(meta["ID"], 16), meta))

    if not args.out:
        args.out = os.path.join(core.logs_dir(), time.strftime("n63_%Y-%m-%d_%H-%M-%S.csv"))
    # The same session as the app's (engine.Session): it borrows the car if
    # the app or another n63read holds it, else holds it and lends it, and
    # reconnects if it drops while logging.
    q = queue.Queue()
    session = engine.Session(args.ip, args.target, args.source, args.timeout, out=q,
                             layouts=core.record_layouts(cat), interval=args.interval,
                             batch=args.batch)
    session.set_channels(chans)
    session.stop_after = args.samples
    session.start_log(args.out)
    print("Logging %d channel(s) to %s - Ctrl-C to stop" % (len(chans), args.out))
    session.start()
    t0 = last_shown = time.time()
    error = None
    try:
        while True:
            try:
                msg = q.get(timeout=0.5)
            except queue.Empty:
                if not session.is_alive():
                    break
                continue
            kind = msg[0]
            if kind == "sample":
                if time.time() - last_shown >= 1.0:
                    last_shown = time.time()
                    print("\r%d samples, %.1f s" % (session.rows, last_shown - t0), end="")
            elif kind == "status" and msg[1] == "reconnecting":
                print("\nconnection lost; reconnecting - Ctrl-C to stop")
            elif kind == "status" and msg[1] == "connected" and session.samples:
                print("\nreconnected")
            elif kind == "status" and msg[1] == "disconnected":
                break
            elif kind == "error":
                error = msg[1]
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        session.stop()
        session.join(10)
        print("wrote %d samples to %s" % (session.rows, args.out))
    if error and not session.rows:
        sys.exit("log: %s" % error.split("\n")[0])


def op_speed(args):
    """Read the same engine channels one per request, then several per
    request, and say how many cycles a second each managed."""
    cat = _catalog(args.csv)
    names = args.names or [n for n in core.BUILTIN_TEMPLATES["Misfire hunt"]
                           if n in cat and not core.is_derived(cat[n])]
    chans = []
    for name in names:
        meta = cat.get(name)
        if not meta or meta.get("MODULE") or core.is_derived(meta):
            sys.exit("not an engine channel: %s" % name)
        chans.append((name, int(meta["ID"], 16), meta))
    results = []
    with _tester(args) as h:
        for batch in (False, True):
            reader = core.ChannelReader({}, args.target, batch=batch)
            cycles, end = 0, time.time() + args.seconds
            while time.time() < end:
                sample = reader.read(h, chans)
                cycles += 1
            answered = sum(1 for v in sample.values() if v[0] is not None)
            results.append((batch, cycles / args.seconds, answered, reader))
    print("%d engine channel(s), %g s each way:\n" % (len(chans), args.seconds))
    for batch, rate, answered, reader in results:
        print("  %-24s %6.1f cycles/s   %d of %d answered" % (
            "several per request" if batch else "one per request", rate, answered, len(chans)))
    batched = results[1][3]
    if batched.batch_off:
        for target, why in sorted(batched.batch_off.items()):
            print("\nDME 0x%02X won't take several identifiers per request (%s);"
                  " the log reads it one at a time." % (target, why))
    elif batched.batched and results[0][1]:
        print("\n%.1fx faster several per request: `n63read log --batch` uses it."
              % (results[1][1] / results[0][1]))


def op_templates(args):
    saved = core.load_templates()
    if not saved:
        print("No templates yet: save one in the app (Templates > Save watch list).")
        return
    for name in sorted(saved, key=str.lower):
        print("%s  (%d)\n    %s" % (name, len(saved[name]), " ".join(saved[name])))


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
            others = update.others_running()
            if others:
                sys.exit("\nnot installing yet: close these first (the installer can't "
                         "replace files they use):\n%s\nthen run `n63read update` again."
                         % "\n".join("  %s (process %d)" % (n, p) for p, n in others))
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
    sp.add_argument("--details", action="store_true",
                    help="also read what was stored with each fault: odometer, "
                         "time, fault class, power state, how often it occurred")
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
    sp.add_argument("--template", help="the channels of a template saved in the app "
                                       "(see `templates`), plus any named")
    sp.add_argument("--batch", action="store_true",
                    help="read engine values several to a request (falls back to one at "
                         "a time if the DME won't; try `speed` first)")
    sp.add_argument("--samples", type=int, default=0,
                    help="stop after this many samples (default: run until Ctrl-C)")
    sp.add_argument("names", nargs="*")
    sp.set_defaults(func=op_log)

    sp = sub.add_parser("speed", help="how fast engine values read, one per request "
                                      "and several per request")
    common(sp)
    sp.add_argument("--target", type=hexint, default=DME_MASTER)
    sp.add_argument("--seconds", type=float, default=10.0, help="per method (default 10)")
    sp.add_argument("names", nargs="*", help="channels (default: the misfire hunt set's "
                                             "engine channels)")
    sp.set_defaults(func=op_speed)

    sp = sub.add_parser("templates", help="list the channel templates saved in the app")
    sp.set_defaults(func=op_templates)

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
