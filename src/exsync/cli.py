"""Command line for exsync.

  exsync -c config.toml folders               list mail folders of primary and archive mailbox
  exsync -c config.toml folders --target      … with Dovecot target folder and status
  exsync -c config.toml bench FOLDER -n 2000  measure EWS export (optionally --write to Dovecot)
  exsync -c config.toml bench FOLDER -r       … including subfolders, structure below --target
  exsync -c config.toml copy [--dry-run]      copy primary and archive mailbox completely (resumable)
  exsync -c config.toml sieve-test            deliver a test mail via LMTP and find its folder
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from email.message import EmailMessage
from email.utils import formatdate

from exsync import bench, config, dovecot, ews, fullcopy
from exsync.progress import Progress


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="exsync", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="config.toml", help="configuration file (TOML)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug output and full tracebacks")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("folders", help="list mail folders of primary and archive mailbox")
    f.add_argument("--no-archive", action="store_true", help="skip the archive mailbox")
    f.add_argument("--target", action="store_true",
                   help="show the Dovecot target folder and whether it already exists")
    f.add_argument("--offline", action="store_true",
                   help="with --target: do not contact Dovecot (delimiter '/', no status)")

    b = sub.add_parser("bench", help="measure EWS export (and optionally IMAP APPEND)")
    b.add_argument("folder", help="e.g. 'Inbox' or 'Archive:Projects'")
    b.add_argument("-n", "--limit", type=int, default=1000,
                   help="maximum total number of mails to fetch (with -r across all folders)")
    b.add_argument("-r", "--recursive", action="store_true",
                   help="include subfolders and recreate them below --target")
    b.add_argument("--per-folder", type=int, default=None,
                   help="with -r: at most this many mails per folder (the newest)")
    b.add_argument("--page-size", type=int, default=100, help="EWS page size")
    b.add_argument("--write", action="store_true", help="write mails to Dovecot")
    b.add_argument("--target", default="exsync-bench", help="Dovecot folder for --write")
    b.add_argument("--total-gb", type=float, default=30.0, help="data volume for the extrapolation")

    c = sub.add_parser("copy", help="copy primary and archive mailbox completely to Dovecot (resumable)")
    c.add_argument("--no-archive", action="store_true", help="skip the archive mailbox")
    c.add_argument("--exclude", action="append", default=[], metavar="FOLDER",
                   help="skip a folder including its subfolders, path as shown by 'folders' "
                   "(repeatable), e.g. 'Deleted Items' or 'Archive:Junk Email'")
    c.add_argument("--dry-run", action="store_true", help="only count what would be copied; write nothing")
    c.add_argument("--state", default=None,
                   help="state file (default: ~/.local/state/exsync/<mailbox>.sqlite3)")
    c.add_argument("--batch", type=int, default=50, help="mails per EWS request")

    s = sub.add_parser("sieve-test", help="deliver a test mail via LMTP and check where it lands")
    s.add_argument("--subject", default="exsync Sieve test")
    s.add_argument("--sender", default="exsync-test@localhost")
    s.add_argument("--wait", type=float, default=2.0, help="seconds to wait before searching")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    cfg = config.load(args.config)

    from exchangelib.errors import UnauthorizedError

    try:
        if args.cmd == "folders":
            return cmd_folders(cfg, include_archive=not args.no_archive, target=args.target,
                               offline=args.offline)
        if args.cmd == "bench":
            return cmd_bench(cfg, args)
        if args.cmd == "copy":
            return cmd_copy(cfg, args)
        return cmd_sieve_test(cfg, args)
    except UnauthorizedError as exc:
        if args.verbose:
            raise
        print(f"EWS login rejected: {exc}\n"
              f"  user: {cfg.ews.username}, auth_type: {cfg.ews.auth_type}\n"
              "  Check: password; auth_type (NTLM instead of basic if the server offers no Basic);\n"
              "  user name as DOMAIN\\user or user@domain. Details with -v.",
              file=sys.stderr)
        return 1


def cmd_folders(cfg: config.Config, include_archive: bool, target: bool = False,
                offline: bool = False) -> int:
    if target and cfg.dovecot is None:
        print("--target requires a [dovecot] section in the configuration", file=sys.stderr)
        return 2
    account = ews.connect(cfg.ews, cfg.ews.get_password())
    try:
        folders = list(ews.mail_folders(account, include_archive=include_archive))
    except Exception as exc:  # noqa: BLE001 – e.g. no archive mailbox or no access
        print(f"Error listing folders: {exc}", file=sys.stderr)
        return 1
    total = sum(mf.total_count or 0 for mf in folders)
    if not target:
        for mf in folders:
            print(f"{mf.total_count or 0:>8}  {mf.label}")
        print(f"{total:>8}  mails in total")
        return 0

    delimiter, existing = "/", None
    if not offline:
        writer = dovecot.ImapWriter(cfg.dovecot, cfg.dovecot.get_password())
        try:
            delimiter, existing = writer.delimiter, writer.existing_folders()
        finally:
            writer.close()
    for line in target_table(folders, delimiter, cfg.dovecot.archive_prefix, existing):
        print(line)
    print(f"{total:>8}  mails in total")
    return 0


def target_table(folders, delimiter: str, archive_prefix: str, existing: set[str] | None) -> list[str]:
    """Lines "count  source → target  [status]" for `folders --target`.

    Status: "exists"/"new" in Dovecot (empty without a connection) and "duplicate" if several
    Exchange folders would end up in the same Dovecot folder.
    """
    rows = [(mf, dovecot.folder_name(mf.source, mf.parts, delimiter, archive_prefix)) for mf in folders]
    counts: dict[str, int] = {}
    for _mf, name in rows:
        counts[name] = counts.get(name, 0) + 1
    width = max((len(mf.label) for mf, _ in rows), default=6)
    lines = [f"{'Mails':>8}  {'Exchange':<{width}}    Dovecot"]
    for mf, name in rows:
        status = []
        if existing is not None:
            status.append("exists" if name in existing else "new")
        if counts[name] > 1:
            status.append("duplicate")
        suffix = f"  [{', '.join(status)}]" if status else ""
        lines.append(f"{mf.total_count or 0:>8}  {mf.label:<{width}} →  {name}{suffix}")
    return lines


def cmd_bench(cfg: config.Config, args) -> int:
    account = ews.connect(cfg.ews, cfg.ews.get_password())
    writer = None
    if args.write:
        if cfg.dovecot is None:
            print("--write requires a [dovecot] section in the configuration", file=sys.stderr)
            return 2
        writer = dovecot.ImapWriter(cfg.dovecot, cfg.dovecot.get_password())
    try:
        res = bench.run(account, args.folder, args.limit, writer=writer, target=args.target,
                        page_size=args.page_size, recursive=args.recursive,
                        per_folder=args.per_folder)
    finally:
        if writer:
            writer.close()
    print(res.report(args.total_gb, show_folders=args.recursive))
    return 0


def cmd_copy(cfg: config.Config, args) -> int:
    if cfg.dovecot is None:
        print("copy requires a [dovecot] section in the configuration", file=sys.stderr)
        return 2
    state_path = args.state or fullcopy.default_state_path(cfg.ews.email, cfg.dovecot.user,
                                                           cfg.dovecot.imap_host)
    account = ews.connect(cfg.ews, cfg.ews.get_password())
    writer = dovecot.ImapWriter(cfg.dovecot, cfg.dovecot.get_password())
    state = fullcopy.State(state_path)
    print(f"State file: {state_path}", flush=True)
    stats = None
    try:
        stats = fullcopy.run(account, writer, state, include_archive=not args.no_archive,
                             exclude=args.exclude, dry_run=args.dry_run, batch=args.batch,
                             progress=Progress(sys.stdout))
    except KeyboardInterrupt:
        print("\nInterrupted. Running the same command again resumes and only transfers missing mails.",
              file=sys.stderr)
        return 130
    finally:
        writer.close()
        state.close()
    print(stats.summary(dry_run=args.dry_run))
    return 1 if stats.errors else 0


def cmd_sieve_test(cfg: config.Config, args) -> int:
    if cfg.dovecot is None:
        print("sieve-test requires a [dovecot] section in the configuration", file=sys.stderr)
        return 2
    msg = EmailMessage()
    msgid = f"<exsync-{uuid.uuid4().hex}@exsync.invalid>"
    msg["From"], msg["To"], msg["Subject"] = args.sender, cfg.dovecot.user, args.subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), msgid
    msg.set_content("Test mail from exsync to check Sieve filtering on LMTP delivery.\n")
    dovecot.lmtp_deliver(cfg.dovecot, msg.as_bytes(), sender=args.sender)
    print(f"Delivered: {msgid}")
    time.sleep(args.wait)
    writer = dovecot.ImapWriter(cfg.dovecot, cfg.dovecot.get_password())
    try:
        hits = writer.find_message_id(msgid)
    finally:
        writer.close()
    print("Found in: " + (", ".join(hits) if hits else "– (not there yet or discarded by Sieve)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
