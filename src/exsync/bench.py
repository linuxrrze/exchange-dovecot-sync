"""Messung: Wie schnell liefert EWS den MIME-Inhalt, wie schnell nimmt Dovecot ihn an?"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

from exsync import dovecot, ews

FIELDS = ("mime_content", "datetime_received", "is_read", "categories", "flag_status", "last_verb")


@dataclass
class BenchResult:
    items: int = 0
    errors: int = 0
    bytes: int = 0
    ews_seconds: float = 0.0
    imap_seconds: float = 0.0
    # je bearbeitetem Ordner: (Quellpfad relativ zum Startordner, Zielordner, Anzahl Mails)
    folders: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def mb(self) -> float:
        return self.bytes / 1e6

    def rate_mb_s(self, seconds: float) -> float:
        return self.mb / seconds if seconds > 0 else 0.0

    def hours_for(self, total_gb: float) -> float | None:
        """Hochrechnung der Dauer für total_gb (EWS + IMAP, sequenziell)."""
        secs = self.ews_seconds + self.imap_seconds
        if not self.bytes or secs <= 0:
            return None
        return total_gb * 1e9 / (self.bytes / secs) / 3600

    def report(self, total_gb: float, show_folders: bool = False) -> str:
        lines = []
        if show_folders:
            width = max((len(src) for src, _, _ in self.folders), default=0)
            lines += [f"{n:>8}  {src:<{width}} →  {dest}" for src, dest, n in self.folders]
            lines.append(f"Ordner: {len(self.folders)}")
        lines += [
            f"Mails: {self.items} ({self.errors} Fehler), {self.mb:.1f} MB",
            (f"EWS:   {self.ews_seconds:.1f} s → {self.rate_mb_s(self.ews_seconds):.2f} MB/s, "
             f"{self.items / self.ews_seconds if self.ews_seconds else 0:.1f} Mails/s"),
        ]
        if self.imap_seconds:
            lines.append(f"IMAP:  {self.imap_seconds:.1f} s → {self.rate_mb_s(self.imap_seconds):.2f} MB/s")
        h = self.hours_for(total_gb)
        if h is not None:
            lines.append(f"Hochrechnung für {total_gb:g} GB: {h:.1f} h (≈ {h / 24:.1f} Tage)")
        return "\n".join(lines)


def run(account, path: str, limit: int, writer: dovecot.ImapWriter | None = None,
        target: str = "exsync-bench", page_size: int = 100, recursive: bool = False,
        per_folder: int | None = None) -> BenchResult:
    """Mails eines Ordners (mit `recursive` auch seiner Unterordner) exportieren und optional
    nach Dovecot schreiben.

    `limit` begrenzt die Gesamtzahl der geladenen Mails, `per_folder` zusätzlich die je Ordner
    (jeweils die neuesten). Unterordner werden unterhalb von `target` nachgebaut.
    """
    start = ews.folder(account, path)
    sources: list[tuple[tuple[str, ...], object]] = [((), start)]
    if recursive:
        sources += [(ews.relative_parts(f, start), f) for f in start.walk() if ews.is_mail_folder(f)]
    delimiter = writer.delimiter if writer is not None else "/"
    res = BenchResult()
    for parts, folder in sources:
        remaining = limit - res.items - res.errors
        if remaining <= 0:
            break
        n = min(remaining, per_folder) if per_folder else remaining
        dest = delimiter.join([target, *(p.replace(delimiter, "_") for p in parts)])
        before = res.items
        _copy(folder, n, writer, dest, page_size, res)
        label = "/".join(parts) if parts else "(Startordner)"
        res.folders.append((label, dest, res.items - before))
    return res


def _copy(folder, n: int, writer, dest: str, page_size: int, res: BenchResult) -> None:
    qs = folder.all().order_by("-datetime_received").only(*FIELDS)
    # Seitengröße nie größer als das Limit, und zusätzlich selbst abschneiden: Slicing allein
    # (qs[:n]) hat bei exchangelib trotzdem eine ganze Seite (100 Mails) geliefert.
    qs.page_size = max(1, min(page_size, n))
    it = itertools.islice(qs, n)
    while True:
        t0 = time.perf_counter()
        try:
            item = next(it)
        except StopIteration:
            res.ews_seconds += time.perf_counter() - t0
            break
        res.ews_seconds += time.perf_counter() - t0
        if not isinstance(item, Exception) and not ews.is_mail_item(item):
            continue  # Kalendereintrag o.ä. in einem Mailordner
        if isinstance(item, Exception) or not getattr(item, "mime_content", None):
            res.errors += 1
            continue
        res.items += 1
        res.bytes += len(item.mime_content)
        if writer is not None:
            t1 = time.perf_counter()
            writer.append(dest, item.mime_content, ews.imap_flags(item), item.datetime_received)
            res.imap_seconds += time.perf_counter() - t1
