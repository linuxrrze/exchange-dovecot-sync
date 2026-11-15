"""Benchmark: how fast does EWS deliver MIME content, how fast does Dovecot accept it?"""

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
    # per processed folder: (source path relative to the start folder, target folder, mail count)
    folders: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def mb(self) -> float:
        return self.bytes / 1e6

    def rate_mb_s(self, seconds: float) -> float:
        return self.mb / seconds if seconds > 0 else 0.0

    def hours_for(self, total_gb: float) -> float | None:
        """Extrapolated duration for total_gb (EWS + IMAP, sequential)."""
        secs = self.ews_seconds + self.imap_seconds
        if not self.bytes or secs <= 0:
            return None
        return total_gb * 1e9 / (self.bytes / secs) / 3600

    def report(self, total_gb: float, show_folders: bool = False) -> str:
        lines = []
        if show_folders:
            width = max((len(src) for src, _, _ in self.folders), default=0)
            lines += [f"{n:>8}  {src:<{width}} →  {dest}" for src, dest, n in self.folders]
            lines.append(f"Folders: {len(self.folders)}")
        lines += [
            f"Mails: {self.items} ({self.errors} errors), {self.mb:.1f} MB",
            (f"EWS:   {self.ews_seconds:.1f} s → {self.rate_mb_s(self.ews_seconds):.2f} MB/s, "
             f"{self.items / self.ews_seconds if self.ews_seconds else 0:.1f} Mails/s"),
        ]
        if self.imap_seconds:
            lines.append(f"IMAP:  {self.imap_seconds:.1f} s → {self.rate_mb_s(self.imap_seconds):.2f} MB/s")
        h = self.hours_for(total_gb)
        if h is not None:
            lines.append(f"Extrapolation for {total_gb:g} GB: {h:.1f} h (≈ {h / 24:.1f} days)")
        return "\n".join(lines)


def run(account, path: str, limit: int, writer: dovecot.ImapWriter | None = None,
        target: str = "exsync-bench", page_size: int = 100, recursive: bool = False,
        per_folder: int | None = None) -> BenchResult:
    """Export the mails of a folder (with `recursive` also of its subfolders) and optionally
    write them to Dovecot.

    `limit` caps the total number of fetched mails, `per_folder` additionally the number per
    folder (the newest each). Subfolders are recreated below `target`.
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
        label = "/".join(parts) if parts else "(start folder)"
        res.folders.append((label, dest, res.items - before))
    return res


def _copy(folder, n: int, writer, dest: str, page_size: int, res: BenchResult) -> None:
    qs = folder.all().order_by("-datetime_received").only(*FIELDS)
    # Page size never larger than the limit, and cut off ourselves: slicing alone (qs[:n]) still
    # returned a whole page (100 mails) with exchangelib.
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
            continue  # calendar item or similar in a mail folder
        if isinstance(item, Exception) or not getattr(item, "mime_content", None):
            res.errors += 1
            continue
        res.items += 1
        res.bytes += len(item.mime_content)
        if writer is not None:
            t1 = time.perf_counter()
            writer.append(dest, item.mime_content, ews.imap_flags(item), item.datetime_received)
            res.imap_seconds += time.perf_counter() - t1
