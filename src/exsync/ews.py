"""Access to Exchange via EWS (exchangelib): connection, folders, flags."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from exsync.config import EwsConfig

# MS-OXOFLAG: PidTagFlagStatus 0x1090 → 2 = flagged, 1 = completed
FLAGGED = 2
# MS-OXOMSG: PidTagLastVerbExecuted 0x1081
VERB_REPLY = (102, 103)  # reply, reply all
VERB_FORWARD = 104
ARCHIVE_PREFIXES = ("archiv", "archive", "online-archiv")
# Folder classes containing mail (calendars, contacts, tasks are skipped)
MAIL_FOLDER_CLASSES = ("IPF.Note",)

_registered = False


def register_props() -> None:
    """Register extended MAPI properties with exchangelib (once)."""
    global _registered
    if _registered:
        return
    from exchangelib import ExtendedProperty, Message

    class FlagStatus(ExtendedProperty):
        property_tag = 0x1090
        property_type = "Integer"

    class LastVerbExecuted(ExtendedProperty):
        property_tag = 0x1081
        property_type = "Integer"

    for name, cls in (("flag_status", FlagStatus), ("last_verb", LastVerbExecuted)):
        try:
            Message.register(name, cls)
        except ValueError:  # already registered
            pass
    _registered = True


def connect(cfg: EwsConfig, password: str):
    register_props()
    from exchangelib import BASIC, DELEGATE, NTLM, Account, Configuration, Credentials
    from exchangelib.protocol import BaseProtocol, FaultTolerance, NoVerifyHTTPAdapter

    if not cfg.verify_tls:
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
    kwargs: dict[str, Any] = {
        "credentials": Credentials(cfg.username, password),
        "auth_type": NTLM if cfg.auth_type == "NTLM" else BASIC,
        # On throttling (ErrorServerBusy) and transient connection errors wait as long as the
        # server asks (default 60 s) and retry, instead of failing on the first error
        "retry_policy": FaultTolerance(max_wait=cfg.max_wait),
    }
    if cfg.server.startswith("http"):
        kwargs["service_endpoint"] = cfg.server
    else:
        kwargs["server"] = cfg.server
    return Account(primary_smtp_address=cfg.email, config=Configuration(**kwargs),
                   autodiscover=False, access_type=DELEGATE)


def folder(account, path: str):
    """Folder by path. "Archive:<path>" (or "Archiv:<path>") addresses the archive mailbox."""
    archive = False
    prefix, sep, rest = path.partition(":")
    if sep and prefix.strip().lower() in ARCHIVE_PREFIXES:
        archive, path = True, rest
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return account.archive_msg_folder_root if archive else account.inbox
    if parts[0].lower() in ("inbox", "posteingang"):
        f = account.archive_inbox if archive else account.inbox
        parts = parts[1:]
    else:
        f = account.archive_msg_folder_root if archive else account.msg_folder_root
    for p in parts:
        f = f / p
    return f


@dataclass
class MailFolder:
    source: str  # "primary" or "archive"
    parts: tuple[str, ...]  # path relative to the root of the respective mailbox
    folder: Any
    total_count: int | None

    @property
    def label(self) -> str:
        path = "/".join(self.parts)
        return f"Archive:{path}" if self.source == "archive" else path


def mail_folders(account, include_archive: bool = True) -> Iterator[MailFolder]:
    """All mail folders of the primary and (optionally) the archive mailbox."""
    roots = [("primary", account.msg_folder_root)]
    if include_archive:
        roots.append(("archive", account.archive_msg_folder_root))
    for source, root in roots:
        for f in root.walk():
            if not is_mail_folder(f):
                continue
            yield MailFolder(source, relative_parts(f, root), f, getattr(f, "total_count", None))


def is_mail_folder(f) -> bool:
    cls = getattr(f, "folder_class", None)
    return bool(cls) and any(cls == c or cls.startswith(c + ".") for c in MAIL_FOLDER_CLASSES)


def relative_parts(f, root) -> tuple[str, ...]:
    names = []
    while f is not None and f.id != root.id:
        names.append(f.name)
        f = f.parent
    return tuple(reversed(names))


def is_mail_item(item) -> bool:
    """Mail or meeting request/response/cancellation? Calendar items, contacts, tasks etc.
    (e.g. deleted ones in "Deleted Items") are not and are not copied."""
    from exchangelib.items import Item, MeetingCancellation, MeetingRequest, MeetingResponse, Message

    if not isinstance(item, Item) or type(item) is Item:
        # Test objects or unknown type: exchangelib returns generic Item objects if only
        # id/changekey were requested. Do not treat as "not a mail"; check again later.
        return True
    return isinstance(item, (Message, MeetingRequest, MeetingResponse, MeetingCancellation))


# ------------------------------------------------------------ Flags

_KEYWORD_BAD = re.compile(r"[^A-Za-z0-9_.-]+")


# Dovecot rejects longer keywords ("Keyword length too long"), setting mail_max_keyword_length
MAX_KEYWORD_LENGTH = 50


def imap_flags(item, max_keyword_length: int = MAX_KEYWORD_LENGTH) -> list[str]:
    """Exchange state of a mail → IMAP flags and keywords."""
    flags = []
    if getattr(item, "is_read", False):
        flags.append("\\Seen")
    if getattr(item, "flag_status", None) == FLAGGED:
        flags.append("\\Flagged")
    verb = getattr(item, "last_verb", None)
    if verb in VERB_REPLY:
        flags.append("\\Answered")
    elif verb == VERB_FORWARD:
        flags.append("$Forwarded")
    for cat in getattr(item, "categories", None) or ():
        kw = category_keyword(cat, max_keyword_length)
        if kw and kw not in flags:
            flags.append(kw)
    return flags


def category_keyword(category: str, max_length: int = MAX_KEYWORD_LENGTH) -> str:
    """Outlook category → IMAP keyword (only ASCII atom characters allowed)."""
    ascii_ = (category.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
              .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue"))
    kw = _KEYWORD_BAD.sub("_", ascii_).strip("_")
    return kw[:max_length].rstrip("_")
