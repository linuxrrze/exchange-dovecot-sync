"""Progress display: a single overwritten line in a terminal, occasional lines otherwise."""

from __future__ import annotations

import shutil
import sys
import time


class Progress:
    def __init__(self, stream=None, tty: bool | None = None, interval: float | None = None):
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty() if tty is None else tty
        # terminal: update often; log file/pipe: at most one line every 30 s
        self.interval = interval if interval is not None else (0.2 if self.tty else 30.0)
        self._last = 0.0
        self._open = False  # is an unfinished line currently shown in the terminal?

    def update(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < self.interval:
            return
        self._last = now
        if self.tty:
            width = shutil.get_terminal_size((120, 24)).columns - 1
            if len(text) > width:  # otherwise the line wraps and cannot be overwritten
                text = text[:width - 1] + "…"
            self.stream.write("\r\x1b[K" + text)
            self._open = True
        else:
            self.stream.write(text + "\n")
        self.stream.flush()

    def line(self, text: str) -> None:
        """Permanent line; replaces an open progress line in a terminal."""
        if self.tty and self._open:
            self.stream.write("\r\x1b[K")
            self._open = False
        self.stream.write(text + "\n")
        self.stream.flush()
        self._last = 0.0

    def close(self) -> None:
        if self.tty and self._open:
            self.stream.write("\n")
            self.stream.flush()
            self._open = False


class Silent(Progress):
    """For tests: shows nothing, collects permanent lines."""

    def __init__(self):
        self.lines: list[str] = []
        self.tty = False

    def update(self, text: str, force: bool = False) -> None:
        pass

    def line(self, text: str) -> None:
        self.lines.append(text)

    def close(self) -> None:
        pass


def eta(done: int, total: int, seconds: float) -> str:
    if done <= 0 or seconds <= 0 or total <= done:
        return ""
    rest = int((total - done) * seconds / done)
    h, rem = divmod(rest, 3600)
    m, s = divmod(rem, 60)
    return f", ~{h}:{m:02d}:{s:02d} left"
