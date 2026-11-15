"""Writing to Dovecot: IMAP APPEND (regular path) and LMTP (new inbox mails with Sieve).

Both also work if Dovecot runs on another host. LMTP over TCP requires an lmtp `inet_listener`
in Dovecot that is reachable only from the sync host.
"""

from __future__ import annotations

import imaplib
import logging
import smtplib
from collections.abc import Sequence
from datetime import datetime
from email.parser import BytesHeaderParser

from exsync.config import DovecotConfig

log = logging.getLogger(__name__)


def normalize_message_id(value) -> str | None:
    """Make a Message-ID comparable: no whitespace/line breaks, in angle brackets."""
    if not value:
        return None
    mid = "".join(str(value).split())
    if not mid:
        return None
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    return mid


def folder_name(source: str, parts: Sequence[str], delimiter: str, archive_prefix: str = "Archiv") -> str:
    """Exchange folder path → Dovecot folder name.

    The inbox of the primary mailbox becomes INBOX, the archive mailbox goes below `archive_prefix`.
    Delimiters inside a folder name are replaced so that no extra hierarchy level is created.
    """
    clean = [p.replace(delimiter, "_") for p in parts]
    if source == "primary" and clean and clean[0].lower() in ("inbox", "posteingang"):
        clean[0] = "INBOX"
    if source == "archive":
        clean = [archive_prefix, *clean]
    return delimiter.join(clean)


class ImapWriter:
    def __init__(self, cfg: DovecotConfig, password: str, client=None):
        if client is None:
            import ssl as ssl_mod

            from imapclient import IMAPClient

            ctx = ssl_mod.create_default_context()
            if not cfg.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl_mod.CERT_NONE
            client = IMAPClient(cfg.imap_host, port=cfg.imap_port, ssl=cfg.ssl,
                                ssl_context=ctx if cfg.ssl else None)
            if not cfg.ssl:
                client.starttls(ctx)
            client.login(cfg.login, password)
        self.client = client
        self.cfg = cfg
        self._known: set[str] = set()
        self.delimiter = self._delimiter()

    def _delimiter(self) -> str:
        for _flags, delim, _name in self.client.list_folders(pattern="INBOX"):
            if delim:
                return delim.decode() if isinstance(delim, bytes) else delim
        return "/"

    def ensure_folder(self, name: str) -> None:
        if name in self._known:
            return
        if not self.client.folder_exists(name):
            # create parent levels
            parts = name.split(self.delimiter)
            for i in range(1, len(parts) + 1):
                sub = self.delimiter.join(parts[:i])
                if not self.client.folder_exists(sub):
                    self.client.create_folder(sub)
        self._known.add(name)

    def append(self, folder: str, mime: bytes, flags: Sequence[str], msg_time: datetime | None) -> None:
        self.ensure_folder(folder)
        self.client.append(folder, mime, flags=tuple(flags), msg_time=msg_time)

    def message_ids(self, folder: str) -> set[str]:
        """Normalized Message-IDs of all mails in a folder (empty if it does not exist)."""
        if not self.client.folder_exists(folder):
            return set()
        info = self.client.select_folder(folder, readonly=True)
        if not info.get(b"EXISTS"):
            return set()
        parser = BytesHeaderParser()
        ids = set()
        uids = self.client.search(["ALL"])
        for i in range(0, len(uids), 1000):  # in chunks to keep the command line short
            chunk = uids[i:i + 1000]
            for data in self.client.fetch(chunk, [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"]).values():
                for key, value in data.items():
                    if key.startswith(b"BODY[") and isinstance(value, bytes):
                        mid = normalize_message_id(parser.parsebytes(value).get("Message-ID"))
                        if mid:
                            ids.add(mid)
        return ids

    def existing_folders(self) -> set[str]:
        return {name for _flags, _delim, name in self.client.list_folders()}

    def find_message_id(self, message_id: str) -> list[str]:
        """Folders containing a mail with this Message-ID."""
        hits = []
        for flags, _delim, name in self.client.list_folders():
            if b"\\Noselect" in flags or b"\\NonExistent" in flags:
                continue
            self.client.select_folder(name, readonly=True)
            if self.client.search(["HEADER", "Message-ID", message_id]):
                hits.append(name)
        return hits

    def close(self) -> None:
        try:
            self.client.logout()
        except (OSError, imaplib.IMAP4.error) as exc:
            log.debug("Logout failed: %s", exc)


def lmtp_deliver(cfg: DovecotConfig, mime: bytes, sender: str = "", factory=smtplib.LMTP) -> None:
    """Deliver a mail to Dovecot via LMTP; Sieve runs as on regular delivery."""
    if not cfg.lmtp_host:
        raise ValueError("LMTP requires dovecot.lmtp_host to be set")
    with factory(cfg.lmtp_host, cfg.lmtp_port) as lmtp:
        refused = lmtp.sendmail(sender, [cfg.user], mime)
    if refused:
        raise RuntimeError(f"LMTP rejected the delivery: {refused}")
