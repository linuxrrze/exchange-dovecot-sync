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
    def fetch(self, ids, only_fields):
        by_id = {m.id: m for m in self.all_mails}
        return [by_id[i] for i, _ck in ids]


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
    assert "keine Mails (ausgelassen): 1" in stats.summary()
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
    assert "zu kopieren: 4" in stats.summary(dry_run=True)


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
    assert "kopiert: 9" in out and "kopiert: 0" in out and len(imap.appended) == 9


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
    p.update("Liste 1/3")
    p.update("Liste 2/3")
    p.line("3 Mails, 0 neu  INBOX")
    p.update("Liste 1/5")
    p.close()
    out = buf.getvalue()
    assert out == ("\r\x1b[KListe 1/3\r\x1b[KListe 2/3\r\x1b[K3 Mails, 0 neu  INBOX\n"
                   "\r\x1b[KListe 1/5\n")
    assert eta(10, 70, 5.0) == ", noch ~0:00:30" and eta(0, 10, 1) == ""


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
    assert any(line.startswith("      2 Mails,       2 neu  Posteingang →  INBOX") for line in prog.lines)
    assert all("kopiert" in line for line in prog.lines if " neu " in line and "0 neu" not in line)


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
