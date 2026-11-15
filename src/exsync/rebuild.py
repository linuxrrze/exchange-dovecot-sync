"""Rebuild a mail from its EWS properties when Exchange cannot export it as MIME.

Exchange refuses the MIME export of some items ("MIME content conversion failed"), e.g. damaged
items from old migrations/PST imports or items with exotic attachments. Their individual
properties (addresses, subject, body, attachments, original internet headers) can usually still
be read, so a replacement mail is built from them. It is marked with the header
`X-Exsync-Reconstructed`. Lost are only details that exist solely in the original MIME (exact
MIME structure, signatures); the original headers (Received, …) are taken over if Exchange still
has them.
"""

from __future__ import annotations

import logging
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, formataddr

log = logging.getLogger(__name__)

REBUILD_HEADER = "X-Exsync-Reconstructed"
REBUILD_FIELDS = ("subject", "sender", "author", "to_recipients", "cc_recipients", "bcc_recipients",
                  "reply_to", "datetime_sent", "datetime_received", "body", "attachments",
                  "message_id", "in_reply_to", "references", "headers", "importance", "is_read",
                  "categories", "flag_status", "last_verb")
INFO_FIELDS = ("subject", "sender", "datetime_received")

# Headers that are generated from the item properties or belong to the old MIME structure; all
# other original headers (Received, Return-Path, X-…, List-…) are taken over.
_OWN_HEADERS = {"from", "sender", "to", "cc", "bcc", "reply-to", "subject", "date", "message-id",
                "in-reply-to", "references", "importance", "mime-version", "content-type",
                "content-transfer-encoding", "content-disposition", "content-id",
                REBUILD_HEADER.lower()}
_MAX_DEPTH = 3  # attached mails within attached mails


class RebuildError(Exception):
    """The mail cannot be rebuilt (the item is unreadable as well)."""


def is_conversion_error(exc: BaseException) -> bool:
    """Is this the server's refusal to export MIME (and not a connection/other error)?"""
    from exchangelib import errors

    return isinstance(exc, (errors.ErrorMimeContentConversionFailed, errors.ErrorContentConversionFailed,
                            errors.ErrorUnsupportedMimeConversion, errors.ErrorUnsupportedTypeForConversion,
                            errors.ErrorMimeContentInvalid, errors.ErrorMimeContentInvalidBase64String))


def rebuild(account, src, reason: str) -> tuple[object, bytes]:
    """Fetch the properties of `src` (has .id/.changekey) and build a MIME mail from them.

    Returns (item, mime). Connection errors (exchangelib TransportError) are passed on, everything
    that makes this one mail unreadable becomes RebuildError.
    """
    from exchangelib.errors import TransportError

    # Damaged items sometimes also refuse single properties: then do without them step by step
    item, missing, last_error = None, [], None
    for drop in ((), ("headers",), ("headers", "body"), ("headers", "body", "attachments")):
        fields = [f for f in REBUILD_FIELDS if f not in drop]
        try:
            item = next(iter(account.fetch(ids=[(src.id, src.changekey)], only_fields=fields)))
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            item = exc
        if not isinstance(item, Exception):
            missing = list(drop)
            break
        last_error = item
    if isinstance(item, Exception) or item is None:
        raise RebuildError(f"reading the properties failed: {last_error}")
    if missing:
        reason = f"{reason} (without {', '.join(missing)})"
    try:
        mime = build_mime(item, reason).as_bytes(policy=policy.SMTP)
    except TransportError:
        raise
    except Exception as exc:
        raise RebuildError(f"building the mail failed: {exc}") from exc
    return item, mime


def describe(account, src) -> str:
    """'date | sender | subject' of an item for error reports (ItemId if even that fails)."""
    from exchangelib.errors import TransportError

    try:
        item = next(iter(account.fetch(ids=[(src.id, src.changekey)], only_fields=list(INFO_FIELDS))))
    except TransportError:
        raise
    except Exception:  # noqa: BLE001
        item = None
    if item is None or isinstance(item, Exception):
        return f"ItemId {src.id}"
    return describe_item(item)


def describe_item(item) -> str:
    dt = getattr(item, "datetime_received", None)
    sender = getattr(item, "sender", None)
    return " | ".join([dt.strftime("%Y-%m-%d %H:%M") if dt else "?",
                       (getattr(sender, "email_address", None) or getattr(sender, "name", None) or "?"),
                       getattr(item, "subject", None) or "(no subject)"])


def build_mime(item, reason: str = "", depth: int = 0) -> EmailMessage:
    msg = EmailMessage(policy=policy.SMTP)
    _set(msg, "From", _addr(getattr(item, "author", None) or getattr(item, "sender", None)))
    sender, author = getattr(item, "sender", None), getattr(item, "author", None)
    if sender and author and _email(sender) != _email(author):
        _set(msg, "Sender", _addr(sender))
    for header, field in (("To", "to_recipients"), ("Cc", "cc_recipients"), ("Bcc", "bcc_recipients"),
                          ("Reply-To", "reply_to")):
        _set(msg, header, ", ".join(a for a in map(_addr, getattr(item, field, None) or ()) if a))
    _set(msg, "Subject", getattr(item, "subject", None) or "")
    dt = getattr(item, "datetime_sent", None) or getattr(item, "datetime_received", None)
    if dt:
        _set(msg, "Date", format_datetime(dt))
    for header, field in (("Message-ID", "message_id"), ("In-Reply-To", "in_reply_to"),
                          ("References", "references")):
        _set(msg, header, " ".join(str(getattr(item, field, None) or "").split()))
    importance = getattr(item, "importance", None)
    if importance and importance != "Normal":
        _set(msg, "Importance", importance)
    for h in getattr(item, "headers", None) or ():  # original internet headers, if still known
        if h.name and h.name.lower() not in _OWN_HEADERS:
            _set(msg, h.name, h.value or "")
    if depth == 0:
        _set(msg, REBUILD_HEADER, f"yes; {reason}" if reason else "yes")

    body = getattr(item, "body", None)
    is_html = type(body).__name__ == "HTMLBody"
    msg.set_content(str(body or ""), subtype="html" if is_html else "plain", cte="quoted-printable")

    from exchangelib.attachments import FileAttachment, ItemAttachment

    attachments = list(getattr(item, "attachments", None) or ())
    related = set()  # indices of attachments already added as inline images
    # inline images referenced via cid: in the HTML body → multipart/related
    for i, att in enumerate(attachments):
        if is_html and isinstance(att, FileAttachment) and att.is_inline and att.content_id:
            data = _content(att)
            if data is not None:
                maintype, subtype = _ctype(att.content_type)
                msg.add_related(data, maintype, subtype, cid=f"<{att.content_id.strip('<>')}>",
                                filename=att.name, disposition="inline")
                related.add(i)
    for i, att in enumerate(attachments):
        if i in related:
            continue
        if isinstance(att, FileAttachment):
            data = _content(att)
            if data is None:
                _note(msg, att.name, "the content could not be read")
                continue
            maintype, subtype = _ctype(att.content_type)
            msg.add_attachment(data, maintype, subtype, filename=att.name or "attachment",
                               disposition="inline" if att.is_inline else "attachment")
        elif isinstance(att, ItemAttachment):
            _attach_item(msg, att, reason, depth)
    return msg


def _attach_item(msg: EmailMessage, att, reason: str, depth: int) -> None:
    from exchangelib.errors import TransportError

    name = att.name or "message"
    try:
        inner = att.item
    except TransportError:
        raise
    except Exception as exc:  # noqa: BLE001
        _note(msg, name, f"the attached item could not be read: {exc}")
        return
    if inner is None:
        _note(msg, name, "the attached item is empty")
        return
    mime = getattr(inner, "mime_content", None)
    if mime:
        part = BytesParser(policy=policy.SMTP).parsebytes(mime)
    elif depth < _MAX_DEPTH:
        part = build_mime(inner, reason, depth + 1)
    else:
        _note(msg, name, "nested too deeply")
        return
    msg.add_attachment(part, filename=f"{name}.eml" if not name.lower().endswith(".eml") else name)


def _content(att) -> bytes | None:
    from exchangelib.errors import TransportError

    try:
        data = att.content
    except TransportError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.debug("Attachment %r unreadable: %s", att.name, exc)
        return None
    return data if isinstance(data, bytes) else (data or "").encode()


def _note(msg: EmailMessage, name: str | None, why: str) -> None:
    msg.add_attachment(f"exsync: attachment {name!r} could not be transferred: {why}\n",
                       filename=f"{name or 'attachment'}.exsync-missing.txt")


def _ctype(value: str | None) -> tuple[str, str]:
    maintype, _, subtype = (value or "").split(";")[0].strip().lower().partition("/")
    if not maintype or not subtype or maintype in ("multipart", "message"):
        return "application", "octet-stream"
    return maintype, subtype


def _email(mailbox) -> str:
    return (getattr(mailbox, "email_address", None) or "").lower()


def _addr(mailbox) -> str:
    if mailbox is None:
        return ""
    email = getattr(mailbox, "email_address", None) or ""
    name = getattr(mailbox, "name", None) or ""
    if email and "@" not in email:  # e.g. Exchange-internal /O=… address without SMTP address
        name, email = name or email, ""
    if not email:
        return formataddr((name, "unknown@invalid")) if name else ""
    return formataddr((name, email))


def _set(msg: EmailMessage, name: str, value: str) -> None:
    if not value:
        return
    try:
        msg[name] = value
    except Exception as exc:  # noqa: BLE001 – a broken header must not stop the whole mail
        log.debug("Header %s dropped (%s): %r", name, exc, value)
