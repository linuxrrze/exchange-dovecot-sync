"""Einmalige Komplettkopie: Haupt- und Archivpostfach rekursiv nach Dovecot.

Fortsetzbar nach Abbruch: Eine Mail gilt als übertragen, wenn
  1. ihre EWS-ItemId in der lokalen Zustandsdatei steht (wird direkt nach jedem erfolgreichen
     IMAP APPEND geschrieben), oder
  2. ihre Message-ID bereits im Zielordner in Dovecot vorkommt (greift auch ohne Zustandsdatei).
Pro Ordner wird zuerst nur die Liste der ItemIds geholt (schnell, 1000 pro Anfrage). Message-IDs
werden nur nachgeladen, wenn der Zielordner schon Mails enthält; den MIME-Inhalt lädt exsync nur
für noch fehlende Mails.

Grenzen: Mails ohne Message-ID werden nur über die Zustandsdatei erkannt. Mehrere Mails mit
derselben Message-ID im selben Ordner werden nur einmal übertragen.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from exsync import bench, dovecot, ews
from exsync.progress import Progress, Silent, eta

# datetime_received ist kein „komplexes“ Feld (kommt direkt mit FindItem) und sorgt dafür, dass
# exchangelib typisierte Objekte (Message, CalendarItem, …) liefert. Nur mit id/changekey nimmt
# exchangelib eine Abkürzung und gibt generische Item-Objekte ohne Typ zurück.
LIST_FIELDS = ("id", "changekey", "datetime_received")
COPY_FIELDS = (*bench.FIELDS, "message_id")


class State:
    """Zustandsdatei (SQLite) mit allen bereits übertragenen EWS-ItemIds."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS done (item_id TEXT PRIMARY KEY, folder TEXT, "
                        "message_id TEXT, copied_at REAL)")
        self.db.commit()

    def done_ids(self) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT item_id FROM done")}

    def mark(self, item_id: str, folder: str, message_id: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO done VALUES (?, ?, ?, ?)",
                        (item_id, folder, message_id, time.time()))
        self.db.commit()  # sofort, damit ein Abbruch höchstens die eine laufende Mail betrifft

    def close(self) -> None:
        self.db.close()


def default_state_path(email: str, dovecot_user: str, imap_host: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    safe = "".join(c if c.isalnum() or c in "@._-" else "_" for c in f"{email}__{dovecot_user}@{imap_host}")
    return base / "exsync" / f"{safe}.sqlite3"


@dataclass
class CopyStats:
    folders: int = 0
    found: int = 0
    skipped: int = 0
    copied: int = 0
    errors: int = 0
    no_mail: int = 0  # Kalendereinträge, Kontakte, Aufgaben … in Mailordnern (nicht kopiert)
    bytes: int = 0
    seconds: float = 0.0

    def summary(self, dry_run: bool = False) -> str:
        verb = "zu kopieren" if dry_run else "kopiert"
        n = self.found - self.skipped if dry_run else self.copied
        line = (f"Ordner: {self.folders}, Mails: {self.found}, bereits vorhanden: {self.skipped}, "
                f"{verb}: {n}, Fehler: {self.errors}")
        if self.no_mail:
            line += f", keine Mails (ausgelassen): {self.no_mail}"
        if not dry_run and self.copied:
            line += f", {self.bytes / 1e6:.1f} MB in {self.seconds:.0f} s"
        return line


def run(account, writer: dovecot.ImapWriter, state: State, include_archive: bool = True,
        exclude: Iterable[str] = (), dry_run: bool = False, batch: int = 50,
        progress: Progress | None = None) -> CopyStats:
    progress = progress or Silent()
    stats = CopyStats()
    done = state.done_ids()
    excl = [e.strip("/").lower() for e in exclude if e.strip("/")]
    t_start = time.perf_counter()
    try:
        for mf in ews.mail_folders(account, include_archive=include_archive):
            label = mf.label.lower()
            if any(label == e or label.startswith(e + "/") for e in excl):
                progress.line(f"übersprungen  {mf.label}")
                continue
            dest = dovecot.folder_name(mf.source, mf.parts, writer.delimiter, writer.cfg.archive_prefix)
            _copy_folder(account, mf, dest, writer, state, done, stats, dry_run, batch, progress)
    finally:
        stats.seconds = time.perf_counter() - t_start
        progress.close()
    return stats


def _copy_folder(account, mf, dest, writer, state, done, stats, dry_run, batch, progress) -> None:
    stats.folders += 1
    name = f"{mf.label} →  {dest}"
    total = mf.total_count or 0

    # 1. Liste holen: nur ItemId/ChangeKey (die Message-ID ist ein „komplexes“ Feld, das FindItem
    #    nicht liefert; exchangelib würde sonst jede Mail einzeln nachladen)
    qs = mf.folder.all().order_by("datetime_received").only(*LIST_FIELDS)
    qs.page_size = 1000
    candidates = []
    found = no_mail = 0
    for n, item in enumerate(qs, start=1):
        progress.update(f"Liste {n}/{total}  {name}")
        if isinstance(item, Exception):
            stats.errors += 1
            continue
        if not ews.is_mail_item(item):
            no_mail += 1
            continue
        found += 1
        if item.id not in done:
            candidates.append(item)

    # 2. Liegen im Zielordner schon Mails, die Kandidaten per Message-ID abgleichen
    #    (greift, wenn die Zustandsdatei fehlt oder unvollständig ist)
    present = set()
    todo = candidates
    if candidates:
        progress.update(f"lese vorhandene Mails in Dovecot  {name}", force=True)
        present = writer.message_ids(dest)
    if candidates and present:
        todo = []
        for i, chunk in enumerate(_chunks(candidates, 200)):
            progress.update(f"Abgleich {i * 200 + len(chunk)}/{len(candidates)}  {name}")
            for src, item in zip(chunk, account.fetch(ids=[(c.id, c.changekey) for c in chunk],
                                                      only_fields=["message_id"]), strict=False):
                mid = None if isinstance(item, Exception) else \
                    dovecot.normalize_message_id(getattr(item, "message_id", None))
                if not (mid and mid in present):
                    todo.append(src)

    stats.found += found
    stats.skipped += found - len(todo)
    stats.no_mail += no_mail
    extra = f"  ({no_mail} keine Mails, ausgelassen)" if no_mail else ""
    summary = f"{found:>7} Mails, {len(todo):>7} neu  {name}{extra}"
    if dry_run or not todo:
        progress.line(summary)
        return

    # 3. Kopieren
    t0, copied, dup, size = time.perf_counter(), 0, 0, 0
    for chunk in _chunks(todo, batch):
        full = account.fetch(ids=[(i.id, i.changekey) for i in chunk], only_fields=list(COPY_FIELDS))
        for src, item in zip(chunk, full, strict=False):
            if not isinstance(item, Exception) and not ews.is_mail_item(item):
                stats.no_mail += 1  # erst beim Laden als Nicht-Mail erkannt
                state.mark(src.id, dest, None)
                done.add(src.id)
                continue
            mime = None if isinstance(item, Exception) else getattr(item, "mime_content", None)
            if not mime:
                stats.errors += 1
                progress.line(f"  Fehler bei {src.id}: {item if isinstance(item, Exception) else 'leer'}")
                continue
            mid = dovecot.normalize_message_id(getattr(item, "message_id", None))
            if mid and mid in present:  # gleiche Message-ID schon im Zielordner
                stats.skipped += 1
                dup += 1
            else:
                writer.append(dest, mime, ews.imap_flags(item), item.datetime_received)
                stats.copied += 1
                stats.bytes += len(mime)
                copied += 1
                size += len(mime)
                if mid:
                    present.add(mid)
            state.mark(src.id, dest, mid)
            done.add(src.id)
            secs = time.perf_counter() - t0
            processed = copied + dup
            progress.update(f"{summary.rstrip()}  [{processed}/{len(todo)}, {size / 1e6:.1f} MB, "
                            f"{copied / secs if secs else 0:.1f} Mails/s{eta(processed, len(todo), secs)}]")
    progress.line(f"{summary}  [{copied} kopiert, {size / 1e6:.1f} MB in {time.perf_counter() - t0:.0f} s]")


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]
