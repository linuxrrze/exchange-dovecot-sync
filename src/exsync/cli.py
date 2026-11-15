"""Kommandozeile für den Prototyp.

  exsync -c config.toml folders               Mailordner von Haupt- und Archivpostfach auflisten
  exsync -c config.toml folders --target      … mit Dovecot-Zielordner und Status
  exsync -c config.toml bench FOLDER -n 2000  EWS-Export messen (optional --write nach Dovecot)
  exsync -c config.toml bench FOLDER -r       … inklusive Unterordner, Struktur unter --target
  exsync -c config.toml copy [--dry-run]      Haupt- und Archivpostfach komplett kopieren (fortsetzbar)
  exsync -c config.toml sieve-test            Testmail per LMTP zustellen und Zielordner suchen
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
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("folders", help="Mailordner von Haupt- und Archivpostfach auflisten")
    f.add_argument("--no-archive", action="store_true")
    f.add_argument("--target", action="store_true",
                   help="Dovecot-Zielordner danebenstellen und prüfen, ob sie schon existieren")
    f.add_argument("--offline", action="store_true",
                   help="mit --target: Dovecot nicht kontaktieren (Trennzeichen '/', ohne Status)")

    b = sub.add_parser("bench", help="EWS-Export (und optional IMAP APPEND) messen")
    b.add_argument("folder", help="z.B. 'Inbox' oder 'Archiv:Projekte'")
    b.add_argument("-n", "--limit", type=int, default=1000,
                   help="maximale Gesamtzahl geladener Mails (bei -r über alle Ordner)")
    b.add_argument("-r", "--recursive", action="store_true",
                   help="Unterordner einbeziehen und unterhalb von --target nachbauen")
    b.add_argument("--per-folder", type=int, default=None,
                   help="mit -r: höchstens so viele Mails je Ordner (die neuesten)")
    b.add_argument("--page-size", type=int, default=100)
    b.add_argument("--write", action="store_true", help="Mails nach Dovecot schreiben")
    b.add_argument("--target", default="exsync-bench", help="Dovecot-Ordner für --write")
    b.add_argument("--total-gb", type=float, default=30.0, help="Datenmenge für die Hochrechnung")

    c = sub.add_parser("copy", help="Haupt- und Archivpostfach komplett nach Dovecot kopieren "
                       "(fortsetzbar)")
    c.add_argument("--no-archive", action="store_true", help="Archivpostfach auslassen")
    c.add_argument("--exclude", action="append", default=[], metavar="ORDNER",
                   help="Ordner samt Unterordnern auslassen, Pfad wie bei 'folders' "
                   "(mehrfach möglich), z.B. 'Gelöschte Elemente' oder 'Archiv:Junk-E-Mail'")
    c.add_argument("--dry-run", action="store_true",
                   help="nur zählen, was kopiert würde; nichts schreiben")
    c.add_argument("--state", default=None,
                   help="Zustandsdatei (Standard: ~/.local/state/exsync/<postfach>.sqlite3)")
    c.add_argument("--batch", type=int, default=50, help="Mails pro EWS-Abruf")

    s = sub.add_parser("sieve-test", help="Testmail per LMTP zustellen und nachsehen, wo sie landet")
    s.add_argument("--subject", default="exsync Sieve-Test")
    s.add_argument("--sender", default="exsync-test@localhost")
    s.add_argument("--wait", type=float, default=2.0)

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
        print(f"EWS-Anmeldung abgelehnt: {exc}\n"
              f"  Benutzer: {cfg.ews.username}, auth_type: {cfg.ews.auth_type}\n"
              "  Prüfen: Passwort; auth_type (NTLM statt basic, falls der Server kein Basic anbietet);\n"
              "  Benutzername als DOMÄNE\\benutzer oder benutzer@domain. Details mit -v.",
              file=sys.stderr)
        return 1


def cmd_folders(cfg: config.Config, include_archive: bool, target: bool = False,
                offline: bool = False) -> int:
    if target and cfg.dovecot is None:
        print("Für --target fehlt der Abschnitt [dovecot] in der Konfiguration", file=sys.stderr)
        return 2
    account = ews.connect(cfg.ews, cfg.ews.get_password())
    try:
        folders = list(ews.mail_folders(account, include_archive=include_archive))
    except Exception as exc:  # noqa: BLE001 – z.B. kein Archiv vorhanden oder kein Zugriff
        print(f"Fehler beim Auflisten: {exc}", file=sys.stderr)
        return 1
    total = sum(mf.total_count or 0 for mf in folders)
    if not target:
        for mf in folders:
            print(f"{mf.total_count or 0:>8}  {mf.label}")
        print(f"{total:>8}  Mails gesamt")
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
    print(f"{total:>8}  Mails gesamt")
    return 0


def target_table(folders, delimiter: str, archive_prefix: str, existing: set[str] | None) -> list[str]:
    """Zeilen „Anzahl  Quelle → Ziel  Status“ für `folders --target`.

    Status: „vorhanden“/„neu“ in Dovecot (ohne Verbindung leer) und „doppelt“, wenn mehrere
    Exchange-Ordner auf denselben Dovecot-Ordner fallen würden.
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
            status.append("vorhanden" if name in existing else "neu")
        if counts[name] > 1:
            status.append("doppelt")
        suffix = f"  [{', '.join(status)}]" if status else ""
        lines.append(f"{mf.total_count or 0:>8}  {mf.label:<{width}} →  {name}{suffix}")
    return lines


def cmd_bench(cfg: config.Config, args) -> int:
    account = ews.connect(cfg.ews, cfg.ews.get_password())
    writer = None
    if args.write:
        if cfg.dovecot is None:
            print("Für --write fehlt der Abschnitt [dovecot] in der Konfiguration", file=sys.stderr)
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
        print("Für copy fehlt der Abschnitt [dovecot] in der Konfiguration", file=sys.stderr)
        return 2
    state_path = args.state or fullcopy.default_state_path(cfg.ews.email, cfg.dovecot.user,
                                                           cfg.dovecot.imap_host)
    account = ews.connect(cfg.ews, cfg.ews.get_password())
    writer = dovecot.ImapWriter(cfg.dovecot, cfg.dovecot.get_password())
    state = fullcopy.State(state_path)
    print(f"Zustandsdatei: {state_path}", flush=True)
    stats = None
    try:
        stats = fullcopy.run(account, writer, state, include_archive=not args.no_archive,
                             exclude=args.exclude, dry_run=args.dry_run, batch=args.batch,
                             progress=Progress(sys.stdout))
    except KeyboardInterrupt:
        print("\nAbgebrochen. Ein erneuter Aufruf setzt fort und überträgt nur noch fehlende Mails.",
              file=sys.stderr)
        return 130
    finally:
        writer.close()
        state.close()
    print(stats.summary(dry_run=args.dry_run))
    return 1 if stats.errors else 0


def cmd_sieve_test(cfg: config.Config, args) -> int:
    if cfg.dovecot is None:
        print("Abschnitt [dovecot] fehlt in der Konfiguration", file=sys.stderr)
        return 2
    msg = EmailMessage()
    msgid = f"<exsync-{uuid.uuid4().hex}@exsync.invalid>"
    msg["From"], msg["To"], msg["Subject"] = args.sender, cfg.dovecot.user, args.subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), msgid
    msg.set_content("Testmail von exsync, um die Sieve-Filterung bei LMTP-Zustellung zu prüfen.\n")
    dovecot.lmtp_deliver(cfg.dovecot, msg.as_bytes(), sender=args.sender)
    print(f"Zugestellt: {msgid}")
    time.sleep(args.wait)
    writer = dovecot.ImapWriter(cfg.dovecot, cfg.dovecot.get_password())
    try:
        hits = writer.find_message_id(msgid)
    finally:
        writer.close()
    print("Gefunden in: " + (", ".join(hits) if hits else "– (noch nicht da oder von Sieve verworfen)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
