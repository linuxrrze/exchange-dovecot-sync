import imaplib
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_prototype import FakeImap, Node

from exsync import config, dovecot, fullcopy
from exsync.config import DovecotConfig


def mail(n, message_id=True):
    mid = f"<m{n}@x>" if message_id else None
    head = f"Message-ID: {mid}\r\n" if mid else ""
    return SimpleNamespace(
        id=f"ID{n}", changekey="ck", message_id=mid,
        mime_content=f"{head}Subject: {n}\r\n\r\nText {n}".encode(),
        datetime_received=datetime(2026, 1, 1, tzinfo=UTC), is_read=True, categories=None,
        flag_status=None, last_verb=None)


class Account(SimpleNamespace):
    no_mime: set = frozenset()  # ItemIds whose MIME export fails ("conversion failed")
    unreadable: set = frozenset()  # … and whose properties cannot be read either
    no_headers: set = frozenset()  # … whose internet headers cannot be read

    def fetch(self, ids, only_fields):
        from exchangelib.errors import ErrorItemNotFound, ErrorMimeContentConversionFailed

        by_id = {m.id: m for m in self.all_mails}
        out = []
        for i, _ck in ids:
            if "mime_content" in only_fields and i in self.no_mime:
                out.append(ErrorMimeContentConversionFailed("MIME content conversion failed."))
            elif "subject" in only_fields and "mime_content" not in only_fields and i in self.no_headers \
                    and "headers" in only_fields:
                out.append(ErrorMimeContentConversionFailed("Content conversion failed."))
            elif "datetime_sent" in only_fields and i in self.unreadable:  # all rebuild attempts
                out.append(ErrorItemNotFound("The specified object was not found in the store."))
            else:
                out.append(by_id[i])
        return out


def mailbox(without_message_id=False):
    projekte = Node("Projekte", msgs=[mail(3), mail(4)])
    inbox = Node("Posteingang", msgs=[mail(1), mail(2, message_id=not without_message_id)],
                 children=[projekte])
    from exchangelib.items import CalendarItem, MeetingRequest

    deleted_appointment = CalendarItem(id="C1", changekey="ck")  # hat keine message_id
    invite = MeetingRequest(id="MR1", changekey="ck", message_id="<invite@x>",
                            mime_content=b"Message-ID: <invite@x>\r\nSubject: Einladung\r\n\r\nx",
                            datetime_received=datetime(2026, 1, 2, tzinfo=UTC))
    trash = Node("Gelöschte Elemente", msgs=[mail(9), deleted_appointment, invite])
    root = Node("Top of Information Store", children=[inbox, trash,
                                                       Node("Kalender", folder_class="IPF.Appointment")])
    a2019 = Node("2019", msgs=[mail(5), mail(6), mail(7)])
    aroot = Node("Top of Information Store", children=[Node("Posteingang", children=[a2019])])
    mails = [m for n in (inbox, projekte, trash, a2019) for m in n.msgs if m is not deleted_appointment]
    return Account(msg_folder_root=root, inbox=inbox, archive_msg_folder_root=aroot,
                   archive_inbox=aroot / "Posteingang", all_mails=mails)


def writer(imap):
    return dovecot.ImapWriter(DovecotConfig(user="u"), "pw", client=imap)


def copied(imap):
    return sorted((f, len(m)) for f, m in imap.store.items())


def test_copy_everything_with_structure(tmp_path):
    imap = FakeImap()
    stats = fullcopy.run(mailbox(), writer(imap), fullcopy.State(tmp_path / "s.db"))
    assert stats.copied == 9 and stats.errors == 0 and stats.no_mail == 1
    assert "not mails (skipped): 1" in stats.summary()
    assert copied(imap) == [("Archiv/Posteingang/2019", 3), ("Gelöschte Elemente", 2),
                            ("INBOX", 2), ("INBOX/Projekte", 2)]


def test_second_run_copies_nothing(tmp_path):
    imap, acc = FakeImap(), mailbox()
    fullcopy.run(acc, writer(imap), fullcopy.State(tmp_path / "s.db"))
    stats = fullcopy.run(acc, writer(imap), fullcopy.State(tmp_path / "s.db"))
    assert stats.copied == 0 and stats.skipped == 9 and len(imap.appended) == 9


def test_abort_then_resume_copies_only_missing(tmp_path):
    imap, acc = FakeImap(), mailbox(without_message_id=True)
    w = writer(imap)
    real_append, calls = w.append, {"n": 0}

    def flaky_append(*a, **kw):
        if calls["n"] == 3:
            raise KeyboardInterrupt
        calls["n"] += 1
        real_append(*a, **kw)

    w.append = flaky_append
    with pytest.raises(KeyboardInterrupt):
        fullcopy.run(acc, w, fullcopy.State(tmp_path / "s.db"))
    assert len(imap.appended) == 3

    stats = fullcopy.run(acc, writer(imap), fullcopy.State(tmp_path / "s.db"))
    assert stats.copied == 6 and stats.skipped == 3
    assert len(imap.appended) == 9  # keine Mail doppelt, auch die ohne Message-ID nicht


def test_resume_without_state_file_uses_message_ids(tmp_path):
    imap, acc = FakeImap(), mailbox()
    fullcopy.run(acc, writer(imap), fullcopy.State(tmp_path / "a.db"))
    stats = fullcopy.run(acc, writer(imap), fullcopy.State(tmp_path / "neu.db"))
    assert stats.copied == 0 and len(imap.appended) == 9


def test_exclude_no_archive_and_dry_run(tmp_path):
    imap = FakeImap()
    stats = fullcopy.run(mailbox(), writer(imap), fullcopy.State(tmp_path / "s.db"),
                         include_archive=False, exclude=["Gelöschte Elemente"], dry_run=True)
    assert stats.folders == 2 and stats.found == 4 and imap.appended == []
    assert "to copy: 4" in stats.summary(dry_run=True)


def test_normalize_message_id():
    assert dovecot.normalize_message_id(" <a@b>\r\n") == "<a@b>"
    assert dovecot.normalize_message_id("a@b") == "<a@b>"
    assert dovecot.normalize_message_id("") is None


def test_default_state_path_is_per_mailbox_and_target(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    p = fullcopy.default_state_path("a.b@fau.de", "u", "mail.example.org")
    assert p.parent == tmp_path / "exsync" and p.name == "a.b@fau.de__u@mail.example.org.sqlite3"


# ------------------------------------------------------------ Passwörter in der Konfiguration


def write_cfg(tmp_path, mode=0o600):
    path = tmp_path / "c.toml"
    path.write_text('[ews]\nserver="x"\nemail="a@x"\nusername="u"\npassword="ews-pw"\n'
                    '[dovecot]\nuser="u"\npassword="imap-pw"\n')
    os.chmod(path, mode)
    return path


def test_passwords_from_config_env_overrides(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("EXSYNC_EWS_PASSWORD", raising=False)
    monkeypatch.setenv("EXSYNC_IMAP_PASSWORD", "env-pw")
    cfg = config.load(write_cfg(tmp_path))
    assert cfg.ews.get_password() == "ews-pw"
    assert cfg.dovecot.get_password() == "env-pw"
    assert "ews-pw" not in repr(cfg)  # Passwörter nicht in Debug-Ausgaben
    assert capsys.readouterr().err == ""


def test_warns_if_config_with_passwords_is_world_readable(tmp_path, capsys):
    config.load(write_cfg(tmp_path, mode=0o644))
    assert "chmod 600" in capsys.readouterr().err


def test_cli_copy_end_to_end(tmp_path, monkeypatch, capsys):
    from exsync import cli

    path = write_cfg(tmp_path)
    acc, imap, real_writer = mailbox(), FakeImap(), dovecot.ImapWriter
    monkeypatch.setattr(cli.ews, "connect", lambda cfg, pw: acc)
    monkeypatch.setattr(cli.dovecot, "ImapWriter", lambda c, pw: real_writer(c, pw, client=imap))
    state = tmp_path / "state.db"
    assert cli.main(["-c", str(path), "copy", "--state", str(state)]) == 0
    assert cli.main(["-c", str(path), "copy", "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert "copied: 9" in out and "copied: 0" in out and len(imap.appended) == 9


def test_message_ids_reads_existing_folder_in_chunks():
    imap = FakeImap()
    w = writer(imap)
    for n in range(1, 2503):
        w.append("Gross", f"Message-ID: <n{n}@x>\r\n\r\nx".encode(), [], None)
    ids = w.message_ids("Gross")
    assert len(ids) == 2502 and "<n2502@x>" in ids
    assert w.message_ids("gibt-es-nicht") == set()


# ------------------------------------------------------------ Fortschritt


def test_progress_tty_overwrites_same_line():
    import io

    from exsync.progress import Progress, eta

    buf = io.StringIO()
    p = Progress(buf, tty=True, interval=0)
    p.update("Listing 1/3")
    p.update("Listing 2/3")
    p.line("3 mails, 0 new  INBOX")
    p.update("Listing 1/5")
    p.close()
    out = buf.getvalue()
    assert out == ("\r\x1b[KListing 1/3\r\x1b[KListing 2/3\r\x1b[K3 mails, 0 new  INBOX\n"
                   "\r\x1b[KListing 1/5\n")
    assert eta(10, 70, 5.0) == ", ~0:00:30 left" and eta(0, 10, 1) == ""


def test_progress_non_tty_prints_rarely():
    import io

    from exsync.progress import Progress

    buf = io.StringIO()
    p = Progress(buf, tty=False)
    for i in range(100):
        p.update(f"Liste {i}/100")
    p.line("fertig")
    assert buf.getvalue().splitlines() == ["Liste 0/100", "fertig"]


def test_copy_reports_progress_lines(tmp_path):
    from exsync.progress import Silent

    prog = Silent()
    fullcopy.run(mailbox(), writer(FakeImap()), fullcopy.State(tmp_path / "s.db"), progress=prog)
    assert any(line.startswith("      2 mails,       2 new  Posteingang →  INBOX") for line in prog.lines)
    assert all("copied" in line for line in prog.lines if " new " in line and "0 new" not in line)


def test_listing_does_not_request_complex_message_id():
    assert "message_id" not in fullcopy.LIST_FIELDS and "message_id" in fullcopy.COPY_FIELDS


def test_listing_requests_a_field_that_keeps_item_types():
    # Nur id/changekey → exchangelib liefert generische Item-Objekte (siehe Kommentar in fullcopy)
    assert set(fullcopy.LIST_FIELDS) - {"id", "changekey"}


def test_generic_items_from_listing_are_still_copied(tmp_path):
    """Liefert die Liste nur generische Item-Objekte, wird trotzdem kopiert; Kalendereinträge
    werden dann beim Laden des Inhalts erkannt und ausgelassen."""
    from exchangelib.items import CalendarItem, Item

    acc = mailbox()
    appointment = CalendarItem(id="C9", changekey="ck")
    acc.all_mails.append(appointment)
    inbox = acc.inbox
    real = [*inbox.msgs, appointment]
    inbox.msgs = [Item(id=m.id, changekey="ck") for m in real]
    imap = FakeImap()
    stats = fullcopy.run(acc, writer(imap), fullcopy.State(tmp_path / "s.db"))
    assert len(imap.store["INBOX"]) == 2 and stats.no_mail >= 1
    again = fullcopy.run(acc, writer(imap), fullcopy.State(tmp_path / "s.db"))
    assert again.copied == 0


def test_exclude_accepts_german_and_english_archive_prefix(tmp_path):
    for prefix in ("Archive:", "Archiv:", "archive:"):
        stats = fullcopy.run(mailbox(), writer(FakeImap()), fullcopy.State(tmp_path / f"{prefix}.db"),
                             exclude=[f"{prefix}Posteingang"], dry_run=True)
        assert stats.found == 6, prefix  # 2 archive folders skipped, 3 mails less


# ------------------------------------------------------------ lost IMAP connection


class DroppingImap(FakeImap):
    """Drops the connection on given APPEND calls, before or after storing the mail."""

    def __init__(self, drop_on=(), store_before_drop=False):
        super().__init__()
        self.drop_on, self.store_before_drop, self.calls, self.logins = set(drop_on), store_before_drop, 0, 1

    def append(self, folder, mime, flags=(), msg_time=None):
        self.calls += 1
        if self.calls in self.drop_on:
            if self.store_before_drop:
                super().append(folder, mime, flags, msg_time)
            raise imaplib.IMAP4.abort("socket error: EOF")
        super().append(folder, mime, flags, msg_time)


def reconnecting_writer(imap):
    def connect():
        imap.logins += 1
        return imap

    w = dovecot.ImapWriter(DovecotConfig(user="u"), "pw", client=imap, connect=connect)
    w.sleep, w.messages = (lambda s: None), []
    w.notify = w.messages.append
    return w


@pytest.mark.parametrize("stored", [False, True])
def test_append_reconnects_without_duplicates(stored):
    imap = DroppingImap(drop_on={2}, store_before_drop=stored)
    w = reconnecting_writer(imap)
    for n in (1, 2, 3):
        w.append("INBOX", mail(n).mime_content, [], None, message_id=f"<m{n}@x>")
    assert len(imap.store["INBOX"]) == 3 and imap.logins == 2 and w.reconnects == 1
    assert "IMAP connection lost during APPEND to INBOX" in w.messages[0]


def test_copy_survives_dropped_connection(tmp_path):
    imap = DroppingImap(drop_on={4}, store_before_drop=True)
    stats = fullcopy.run(mailbox(), reconnecting_writer(imap), fullcopy.State(tmp_path / "s.db"))
    assert stats.errors == 0 and stats.copied + stats.skipped == 9 and len(imap.appended) == 9


def test_copy_gives_up_on_one_mail_then_aborts_if_dovecot_stays_down(tmp_path):
    # mail 1 fails 4 times (1 + 3 retries) → error, continue; afterwards everything fails → abort
    imap = DroppingImap(drop_on={1, 2, 3, 4})
    w = reconnecting_writer(imap)
    stats = fullcopy.run(mailbox(), w, fullcopy.State(tmp_path / "s.db"))
    assert stats.errors == 1 and stats.copied == 8

    down = DroppingImap(drop_on=range(1, 1000))
    with pytest.raises(imaplib.IMAP4.abort):
        fullcopy.run(mailbox(), reconnecting_writer(down), fullcopy.State(tmp_path / "t.db"))
    assert down.calls == 3 * 4 and down.appended == []


def test_ews_connect_retries_on_throttling(monkeypatch):
    import exchangelib
    from exchangelib.protocol import FaultTolerance

    from exsync import ews
    from exsync.config import EwsConfig

    seen = {}
    monkeypatch.setattr(exchangelib, "Account", lambda **kw: seen.update(kw))
    ews.connect(EwsConfig(server="ex.example.org", email="a@x", username="D\\u", max_wait=900), "pw")
    policy = seen["config"].retry_policy
    assert isinstance(policy, FaultTolerance) and policy.max_wait == 900 and not policy.fail_fast


# ------------------------------------------------------------ MIME export fails: rebuild


def test_rebuild_builds_mail_from_properties():
    from email import message_from_bytes, policy

    from exchangelib import HTMLBody, Mailbox, Message
    from exchangelib.attachments import FileAttachment, ItemAttachment
    from exchangelib.properties import MessageHeader

    from exsync import rebuild

    inner = Message(subject="Original", body="innen", sender=Mailbox(name="X", email_address="x@y"))
    item = Message(
        subject="Grüße", sender=Mailbox(name="Jörg Müller", email_address="joerg@x"),
        to_recipients=[Mailbox(name="A, B", email_address="a@x"), Mailbox(name="Intern", email_address="/O=FAU")],
        datetime_sent=datetime(2019, 3, 1, 12, tzinfo=UTC), message_id="<abc@x>",
        body=HTMLBody('<p>Hallo <img src="cid:img1"></p>'),
        headers=[MessageHeader(name="Received", value="from a by b"),
                 MessageHeader(name="Content-Type", value="text/plain")],
        attachments=[FileAttachment(name="bild.png", content=b"PNG", content_type="image/png",
                                    is_inline=True, content_id="img1"),
                     FileAttachment(name="doc.pdf", content=b"%PDF", content_type="application/pdf"),
                     ItemAttachment(name="Fwd", item=inner)])
    raw = rebuild.build_mime(item, "MIME content conversion failed.").as_bytes(policy=policy.SMTP)
    msg = message_from_bytes(raw, policy=policy.default)
    assert msg["Subject"] == "Grüße" and msg["Message-ID"] == "<abc@x>"
    assert msg["From"] == "Jörg Müller <joerg@x>" and "Intern <unknown@invalid>" in msg["To"]
    assert msg["Received"] == "from a by b" and msg["X-Exsync-Reconstructed"].startswith("yes")
    assert msg["Content-Type"].startswith("multipart/mixed") and msg.get_all("Content-Type") == [msg["Content-Type"]]
    parts = {p.get_content_type(): p for p in msg.walk()}
    assert "cid:img1" in parts["text/html"].get_content()
    assert parts["image/png"]["Content-ID"] == "<img1>" and parts["image/png"].get_content() == b"PNG"
    assert parts["application/pdf"].get_filename() == "doc.pdf"
    assert parts["message/rfc822"].get_content()["Subject"] == "Original"


def test_copy_rebuilds_mail_when_mime_export_fails(tmp_path):
    imap, acc = FakeImap(), mailbox()
    acc.no_mime = {"ID2"}
    acc.all_mails[1].body, acc.all_mails[1].subject = "Text 2", "Zwei"
    stats = fullcopy.run(acc, writer(imap), fullcopy.State(tmp_path / "s.db"))
    assert stats.copied == 9 and stats.rebuilt == 1 and stats.errors == 0 and not stats.failed
    rebuilt = [m for m in imap.store["INBOX"] if b"X-Exsync-Reconstructed" in m]
    assert len(rebuilt) == 1 and b"Message-ID: <m2@x>" in rebuilt[0] and b"Subject: Zwei" in rebuilt[0]


def test_rebuild_does_without_unreadable_properties(tmp_path):
    imap, acc = FakeImap(), mailbox()
    acc.no_mime = acc.no_headers = {"ID1"}
    stats = fullcopy.run(acc, writer(imap), fullcopy.State(tmp_path / "s.db"))
    assert stats.rebuilt == 1 and stats.copied == 9
    [m] = [m for m in imap.store["INBOX"] if b"X-Exsync-Reconstructed" in m]
    assert b"(without headers)" in m


def test_copy_skips_unrecoverable_mail_permanently(tmp_path):
    imap, acc = FakeImap(), mailbox()
    acc.no_mime = acc.unreadable = {"ID3"}
    acc.all_mails[2].subject, acc.all_mails[2].sender = "Kaputt", SimpleNamespace(email_address="s@x")
    state = fullcopy.State(tmp_path / "s.db")
    stats = fullcopy.run(acc, writer(imap), state)
    assert stats.copied == 8 and stats.errors == 0
    assert stats.failed == ["Posteingang/Projekte: 2026-01-01 00:00 | s@x | Kaputt"]
    assert "could not be copied: 1" in stats.summary()
    [(item_id, folder, _info, reason)] = state.failed()
    assert (item_id, folder) == ("ID3", "INBOX/Projekte") and "conversion failed" in reason

    again = fullcopy.run(acc, writer(imap), state)  # not tried again
    assert again.copied == 0 and again.failed == [] and again.skipped == 9

    acc.no_mime = acc.unreadable = set()
    assert state.forget_failed() == 1 and state.failed() == []
    assert fullcopy.run(acc, writer(imap), state).copied == 1


def test_cli_failed_lists_and_retries(tmp_path, monkeypatch, capsys):
    from exsync import cli

    state = fullcopy.State(tmp_path / "state.db")
    state.mark_failed("ID9", "INBOX", "2019-01-01 10:00 | a@x | Kaputt", "MIME content conversion failed.")
    state.close()
    path = write_cfg(tmp_path)
    assert cli.main(["-c", str(path), "failed", "--state", str(tmp_path / "state.db")]) == 0
    assert "INBOX: 2019-01-01 10:00 | a@x | Kaputt" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "failed", "--state", str(tmp_path / "state.db"), "--retry"]) == 0
    assert "Forgotten: 1" in capsys.readouterr().out
    assert fullcopy.State(tmp_path / "state.db").done_ids() == set()
