"""Command line for exsync.

  exsync -c config.toml folders               list mail folders of primary and archive mailbox
  exsync -c config.toml folders --target      … with Dovecot target folder and status
  exsync -c config.toml bench FOLDER -n 2000  measure EWS export (optionally --write to Dovecot)
  exsync -c config.toml bench FOLDER -r       … including subfolders, structure below --target
  exsync -c config.toml copy [--dry-run]      copy primary and archive mailbox completely (resumable)
  exsync -c config.toml failed [--retry]      mails copy could not transfer (MIME export and rebuild failed)
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

    fl = sub.add_parser("failed", help="list mails that copy could not transfer (skipped permanently)")
    fl.add_argument("--state", default=None, help="state file (default as for copy)")
    fl.add_argument("--retry", action="store_true",
                    help="forget them, so that the next copy run tries them again")

    s = sub.add_parser("sieve-test", help="deliver a test mail via LMTP and check where it lands")
    s.add_argument("--subject", default="exsync Sieve test")
    s.add_argument("--sender", default="exsync-test@localhost")
    s.add_argument("--wait", type=float, default=2.0, help="seconds to wait before searching")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    # exchangelib announces each wait on throttling ("Server requested back off …") as a warning
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
        if args.cmd == "failed":
            return cmd_failed(cfg, args)
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
    from exchangelib.errors import ErrorServerBusy, RateLimitError, TransportError

    if cfg.dovecot is None:
        print("copy requires a [dovecot] section in the configuration", file=sys.stderr)
        return 2
    state_path = _state_path(cfg, args)
    account = ews.connect(cfg.ews, cfg.ews.get_password())
    writer = dovecot.ImapWriter(cfg.dovecot, cfg.dovecot.get_password())
    state = fullcopy.State(state_path)
    print(f"State file: {state_path}", flush=True)
    progress = Progress(sys.stdout)
    writer.notify = progress.line
    log_handler = _log_via(progress)
    stats = None
    resume = "Running the same command again resumes and only transfers missing mails."
    try:
        stats = fullcopy.run(account, writer, state, include_archive=not args.no_archive,
                             exclude=args.exclude, dry_run=args.dry_run, batch=args.batch,
                             progress=progress)
    except KeyboardInterrupt:
        print(f"\nInterrupted. {resume}", file=sys.stderr)
        return 130
    except (ErrorServerBusy, RateLimitError, TransportError) as exc:
        if args.verbose:
            raise
        print(f"\nAborted: Exchange did not answer within {cfg.ews.max_wait} s ({exc}). "
              f"Try again later or raise ews.max_wait. {resume}", file=sys.stderr)
        return 1
    except dovecot.CONNECTION_ERRORS as exc:
        if args.verbose:
            raise
        print(f"\nAborted: IMAP connection to {cfg.dovecot.imap_host} lost and reconnecting failed "
              f"({exc or type(exc).__name__}). Check the Dovecot log. {resume}", file=sys.stderr)
        return 1
    finally:
        logging.getLogger().removeHandler(log_handler)
        writer.close()
        state.close()
    print(stats.summary(dry_run=args.dry_run))
    if stats.failed:
        print("Could not be copied (MIME export and rebuild failed; not tried again, "
              "see 'exsync failed'):")
        for line in stats.failed:
            print(f"  {line}")
    return 1 if stats.errors else 0


class _ProgressLogHandler(logging.Handler):
    def __init__(self, progress: Progress):
        super().__init__()
        self.progress = progress

    def emit(self, record: logging.LogRecord) -> None:
        self.progress.line(f"  {record.levelname}: {record.getMessage()}")


def _log_via(progress: Progress) -> logging.Handler:
    """Log messages (e.g. exchangelib's "Server requested back off …") as separate lines
    instead of appending them to the progress line on stderr. Returns the handler to remove."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "stream", None) in (sys.stderr, sys.stdout):
            root.removeHandler(h)
    handler = _ProgressLogHandler(progress)
    root.addHandler(handler)
    return handler


def _state_path(cfg: config.Config, args):
    return args.state or fullcopy.default_state_path(cfg.ews.email, cfg.dovecot.user, cfg.dovecot.imap_host)


def cmd_failed(cfg: config.Config, args) -> int:
    if cfg.dovecot is None:
        print("failed requires a [dovecot] section in the configuration", file=sys.stderr)
        return 2
    state = fullcopy.State(_state_path(cfg, args))
    try:
        rows = state.failed()
        for _item_id, folder, info, reason in rows:
            print(f"{folder}: {info}\n    {reason}")
        print(f"{len(rows)} mails could not be copied")
        if args.retry and rows:
            print(f"Forgotten: {state.forget_failed()}; the next copy run tries them again.")
    finally:
        state.close()
    return 0


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
