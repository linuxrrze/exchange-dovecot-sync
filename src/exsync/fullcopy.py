"""One-time full copy: primary and archive mailbox recursively to Dovecot.

Resumable after an interruption. A mail counts as transferred if
  1. its EWS ItemId is in the local state file (written right after every successful
     IMAP APPEND), or
  2. its Message-ID already exists in the Dovecot target folder (works without a state file).
For each folder only the list of ItemIds is fetched first (fast, 1000 per request). Message-IDs
are only fetched if the target folder already contains mails; MIME content is only downloaded
for mails that are still missing.

Limitations: mails without a Message-ID are only recognised via the state file. Several mails
with the same Message-ID in the same folder are transferred only once.
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

# datetime_received is not a "complex" field (returned directly by FindItem) and makes exchangelib
# return typed objects (Message, CalendarItem, …). With only id/changekey exchangelib takes a
# shortcut and returns generic Item objects without a type.
LIST_FIELDS = ("id", "changekey", "datetime_received")
COPY_FIELDS = (*bench.FIELDS, "message_id")


class State:
    """State file (SQLite) with all EWS ItemIds that have already been transferred."""

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
        self.db.commit()  # immediately, so an interruption affects at most the current mail

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
    no_mail: int = 0  # calendar items, contacts, tasks … in mail folders (not copied)
    bytes: int = 0
    seconds: float = 0.0

    def summary(self, dry_run: bool = False) -> str:
        verb = "to copy" if dry_run else "copied"
        n = self.found - self.skipped if dry_run else self.copied
        line = (f"Folders: {self.folders}, mails: {self.found}, already present: {self.skipped}, "
                f"{verb}: {n}, errors: {self.errors}")
        if self.no_mail:
            line += f", not mails (skipped): {self.no_mail}"
        if not dry_run and self.copied:
            line += f", {self.bytes / 1e6:.1f} MB in {self.seconds:.0f} s"
        return line


def run(account, writer: dovecot.ImapWriter, state: State, include_archive: bool = True,
        exclude: Iterable[str] = (), dry_run: bool = False, batch: int = 50,
        progress: Progress | None = None) -> CopyStats:
    progress = progress or Silent()
    stats = CopyStats()
    done = state.done_ids()
    excl = [_normalize_label(e) for e in exclude if e.strip("/")]
    t_start = time.perf_counter()
    try:
        for mf in ews.mail_folders(account, include_archive=include_archive):
            label = mf.label.lower()
            if any(label == e or label.startswith(e + "/") for e in excl):
                progress.line(f"skipped  {mf.label}")
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

    # 1. Fetch the list without the Message-ID (a "complex" field that FindItem does not return;
    #    exchangelib would otherwise fetch every mail individually)
    qs = mf.folder.all().order_by("datetime_received").only(*LIST_FIELDS)
    qs.page_size = 1000
    candidates = []
    found = no_mail = 0
    for n, item in enumerate(qs, start=1):
        progress.update(f"Listing {n}/{total}  {name}")
        if isinstance(item, Exception):
            stats.errors += 1
            continue
        if not ews.is_mail_item(item):
            no_mail += 1
            continue
        found += 1
        if item.id not in done:
            candidates.append(item)

    # 2. If the target folder already contains mails, compare the candidates by Message-ID
    #    (covers a missing or incomplete state file)
    present = set()
    todo = candidates
    if candidates:
        progress.update(f"reading existing mails in Dovecot  {name}", force=True)
        present = writer.message_ids(dest)
    if candidates and present:
        todo = []
        for i, chunk in enumerate(_chunks(candidates, 200)):
            progress.update(f"Comparing {i * 200 + len(chunk)}/{len(candidates)}  {name}")
            for src, item in zip(chunk, account.fetch(ids=[(c.id, c.changekey) for c in chunk],
                                                      only_fields=["message_id"]), strict=False):
                mid = None if isinstance(item, Exception) else \
                    dovecot.normalize_message_id(getattr(item, "message_id", None))
                if not (mid and mid in present):
                    todo.append(src)

    stats.found += found
    stats.skipped += found - len(todo)
    stats.no_mail += no_mail
    extra = f"  ({no_mail} not mails, skipped)" if no_mail else ""
    summary = f"{found:>7} mails, {len(todo):>7} new  {name}{extra}"
    if dry_run or not todo:
        progress.line(summary)
        return

    # 3. Copy
    t0, copied, dup, size = time.perf_counter(), 0, 0, 0
    for chunk in _chunks(todo, batch):
        full = account.fetch(ids=[(i.id, i.changekey) for i in chunk], only_fields=list(COPY_FIELDS))
        for src, item in zip(chunk, full, strict=False):
            if not isinstance(item, Exception) and not ews.is_mail_item(item):
                stats.no_mail += 1  # only recognised as non-mail when loading
                state.mark(src.id, dest, None)
                done.add(src.id)
                continue
            mime = None if isinstance(item, Exception) else getattr(item, "mime_content", None)
            if not mime:
                stats.errors += 1
                progress.line(f"  error for {src.id}: {item if isinstance(item, Exception) else 'empty'}")
                continue
            mid = dovecot.normalize_message_id(getattr(item, "message_id", None))
            if mid and mid in present:  # same Message-ID already in the target folder
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
                            f"{copied / secs if secs else 0:.1f} mails/s{eta(processed, len(todo), secs)}]")
    progress.line(f"{summary}  [{copied} copied, {size / 1e6:.1f} MB in {time.perf_counter() - t0:.0f} s]")


def _normalize_label(path: str) -> str:
    """Folder path for --exclude: lower case, "Archiv:"/"Online-Archiv:" → "archive:"."""
    path = path.strip("/")
    prefix, sep, rest = path.partition(":")
    if sep and prefix.strip().lower() in ews.ARCHIVE_PREFIXES:
        path = "archive:" + rest.strip("/")
    return path.lower()


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]
