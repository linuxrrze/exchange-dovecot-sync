from datetime import UTC, datetime
from types import SimpleNamespace

from exsync import bench, dovecot, ews
from exsync.config import DovecotConfig

# ------------------------------------------------------------ Fakes


class Node:
    """Exchange-Ordner mit Unterordnern, walk(), parent und Mails."""

    def __init__(self, name, folder_class="IPF.Note", msgs=(), children=()):
        self.name, self.folder_class, self.msgs = name, folder_class, list(msgs)
        self.id = f"id-{id(self)}"
        self.parent = None
        self.children = {c.name: c for c in children}
        for c in children:
            c.parent = self
        self.total_count = len(self.msgs)

    def __truediv__(self, name):
        return self.children[name]

    def walk(self):
        for c in self.children.values():
            yield c
            yield from c.walk()

    def all(self):
        return Query(self.msgs)


class Query(list):
    """Wie exchangelib: Slicing begrenzt die Anzahl nicht verlässlich (liefert alles)."""

    page_size = None

    def __getitem__(self, key):
        if isinstance(key, slice):
            return Query(self)
        return super().__getitem__(key)

    def order_by(self, *a):
        return self

    def only(self, *a):
        return self


def account():
    inbox = Node("Posteingang", msgs=[msg(1)], children=[Node("Projekte")])
    root = Node("Top of Information Store", children=[
        inbox, Node("Kalender", folder_class="IPF.Appointment"), Node("Gesendet")])
    aroot = Node("Top of Information Store", children=[
        Node("Posteingang", children=[Node("2019", msgs=[msg(2), msg(3)])])])
    return SimpleNamespace(msg_folder_root=root, inbox=inbox, archive_msg_folder_root=aroot,
                           archive_inbox=aroot / "Posteingang")


def msg(i, **kw):
    base = {"mime_content": b"Subject: x\r\n\r\n" + b"x" * 100,
            "datetime_received": datetime(2026, 1, i, tzinfo=UTC),
            "is_read": True, "categories": None, "flag_status": None, "last_verb": None}
    base.update(kw)
    return SimpleNamespace(**base)


class FakeImap:
    def __init__(self):
        self.folders = {"INBOX"}
        self.appended = []
        self.store: dict[str, list[bytes]] = {}
        self.selected = None

    def list_folders(self, pattern="*"):
        return [((), b"/", f) for f in sorted(self.folders)]

    def folder_exists(self, name):
        return name in self.folders

    def create_folder(self, name):
        self.folders.add(name)

    def append(self, folder, mime, flags=(), msg_time=None):
        self.appended.append((folder, flags))
        self.store.setdefault(folder, []).append(mime)

    def select_folder(self, name, readonly=False):
        self.selected = name
        return {b"EXISTS": len(self.store.get(name, []))}

    def search(self, criteria):
        assert criteria == ["ALL"]
        return list(range(1, len(self.store.get(self.selected, [])) + 1))

    def fetch(self, messages, parts):
        # wie imapclient: nur Listen von UIDs (bzw. einzelne Zahlen), keine Bereiche wie "1:*"
        if isinstance(messages, str):
            int(messages)
        wanted = set(messages) if isinstance(messages, list) else {int(messages)}
        out = {}
        for i, mime in enumerate(self.store.get(self.selected, []), start=1):
            if i not in wanted:
                continue
            header = b"".join(line + b"\r\n" for line in mime.split(b"\r\n")
                              if line.lower().startswith(b"message-id:"))
            out[i] = {b'BODY[HEADER.FIELDS ("MESSAGE-ID")]': header + b"\r\n"}
        return out

    def logout(self):
        pass


# ------------------------------------------------------------ Tests


def test_mail_folders_primary_and_archive_skip_calendar():
    labels = [mf.label for mf in ews.mail_folders(account())]
    assert labels == ["Posteingang", "Posteingang/Projekte", "Gesendet",
                      "Archive:Posteingang", "Archive:Posteingang/2019"]


def test_folder_path_resolution():
    acc = account()
    assert ews.folder(acc, "Inbox") is acc.inbox
    assert ews.folder(acc, "Archiv:Inbox/2019") is acc.archive_inbox / "2019"
    assert ews.folder(acc, "Gesendet") is acc.msg_folder_root / "Gesendet"


def test_imap_flags():
    m = msg(1, is_read=True, flag_status=ews.FLAGGED, last_verb=102, categories=["Wichtig", "Grün Projekt"])
    assert ews.imap_flags(m) == ["\\Seen", "\\Flagged", "\\Answered", "Wichtig", "Gruen_Projekt"]
    assert ews.imap_flags(msg(1, is_read=False, last_verb=104)) == ["$Forwarded"]


def test_dovecot_folder_name():
    assert dovecot.folder_name("primary", ["Posteingang", "Projekte"], "/") == "INBOX/Projekte"
    assert dovecot.folder_name("archive", ["Posteingang", "a.b"], ".") == "Archiv.Posteingang.a_b"


def test_bench_with_writer_creates_nested_folder():
    imap = FakeImap()
    writer = dovecot.ImapWriter(DovecotConfig(user="u"), "pw", client=imap)
    res = bench.run(account(), "Archiv:Inbox/2019", limit=10, writer=writer, target="Archiv/Test")
    assert res.items == 2 and res.errors == 0 and res.bytes > 0
    assert {"Archiv", "Archiv/Test"} <= imap.folders
    assert imap.appended[0] == ("Archiv/Test", ("\\Seen",))
    assert "Extrapolation" in res.report(30)


def test_lmtp_deliver_uses_dovecot_user():
    sent = {}

    class FakeLmtp:
        def __init__(self, host, port):
            sent["addr"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def sendmail(self, sender, rcpts, mime):
            sent["rcpts"] = rcpts
            return {}

    cfg = DovecotConfig(user="benutzer", lmtp_host="dovecot", lmtp_port=24)
    dovecot.lmtp_deliver(cfg, b"x", factory=FakeLmtp)
    assert sent == {"addr": ("dovecot", 24), "rcpts": ["benutzer"]}


# ------------------------------------------------------------ folders --target


def test_target_table_status_and_duplicates():
    from exsync.cli import target_table

    folders = list(ews.mail_folders(account()))
    folders.append(ews.MailFolder("primary", ("a/b",), None, 1))
    folders.append(ews.MailFolder("primary", ("a_b",), None, 2))
    lines = target_table(folders, "/", "Archiv", existing={"INBOX", "Archiv/Posteingang"})
    text = "\n".join(lines)
    assert "Posteingang/Projekte" in text and "→  INBOX/Projekte  [new]" in text
    assert "→  INBOX  [exists]" in text
    assert "Archive:Posteingang/2019" in text and "→  Archiv/Posteingang/2019  [new]" in text
    assert text.count("[new, duplicate]") == 2  # "a/b" and "a_b" both end up in "a_b"


def test_target_table_offline_has_no_status():
    from exsync.cli import target_table

    lines = target_table(list(ews.mail_folders(account())), ".", "Archiv", existing=None)
    assert "→  INBOX.Projekte" in "\n".join(lines)
    assert not any("[" in line for line in lines[1:])


def test_cli_folders_target(tmp_path, monkeypatch, capsys):
    from exsync import cli

    cfg = tmp_path / "c.toml"
    cfg.write_text('[ews]\nserver="x"\nemail="a@x"\nusername="u"\n[dovecot]\nuser="u"\n')
    monkeypatch.setenv("EXSYNC_EWS_PASSWORD", "pw")
    monkeypatch.setenv("EXSYNC_IMAP_PASSWORD", "pw")
    monkeypatch.setattr(cli.ews, "connect", lambda cfg, pw: account())
    imap, real_writer = FakeImap(), dovecot.ImapWriter
    monkeypatch.setattr(cli.dovecot, "ImapWriter", lambda c, pw: real_writer(c, pw, client=imap))
    assert cli.main(["-c", str(cfg), "folders", "--target"]) == 0
    out = capsys.readouterr().out
    assert "→  INBOX  [exists]" in out and "mails in total" in out


def test_cli_folders_target_needs_dovecot_section(tmp_path, capsys):
    from exsync import cli

    cfg = tmp_path / "c.toml"
    cfg.write_text('[ews]\nserver="x"\nemail="a@x"\nusername="u"\n')
    assert cli.main(["-c", str(cfg), "folders", "--target"]) == 2


def test_cli_reports_rejected_ews_login(tmp_path, monkeypatch, capsys):
    from exchangelib.errors import UnauthorizedError

    from exsync import cli

    cfg = tmp_path / "c.toml"
    cfg.write_text('[ews]\nserver="x"\nemail="a@x"\nusername="DOM\\\\u"\n')
    monkeypatch.setenv("EXSYNC_EWS_PASSWORD", "pw")

    def reject(cfg, pw):
        raise UnauthorizedError("Invalid credentials for https://x/EWS/Exchange.asmx")

    monkeypatch.setattr(cli.ews, "connect", reject)
    assert cli.main(["-c", str(cfg), "folders"]) == 1
    err = capsys.readouterr().err
    assert "EWS login rejected" in err and "DOM\\u" in err and "NTLM" in err


def test_bench_respects_limit_even_if_slicing_does_not():
    acc = account()
    acc.archive_inbox.children["2019"].msgs = [msg(i % 28 + 1) for i in range(30)]
    res = bench.run(acc, "Archiv:Inbox/2019", limit=10)
    assert res.items == 10


# ------------------------------------------------------------ bench --recursive


def archive_tree():
    """Archiv: Posteingang (1 Mail) mit Unterordnern 2019 (2) → Q1 (3) und Kalender (ignorieren)."""
    q1 = Node("Q1", msgs=[msg(i) for i in range(1, 4)])
    y2019 = Node("2019", msgs=[msg(4), msg(5)], children=[q1])
    cal = Node("Termine", folder_class="IPF.Appointment", msgs=[msg(6)])
    inbox = Node("Posteingang", msgs=[msg(7)], children=[y2019, cal])
    aroot = Node("Top of Information Store", children=[inbox])
    return SimpleNamespace(msg_folder_root=Node("root"), inbox=Node("Inbox"),
                           archive_msg_folder_root=aroot, archive_inbox=inbox)


def test_bench_recursive_rebuilds_structure_and_skips_non_mail():
    imap = FakeImap()
    writer = dovecot.ImapWriter(DovecotConfig(user="u"), "pw", client=imap)
    res = bench.run(archive_tree(), "Archiv:Inbox", limit=100, writer=writer, target="T",
                    recursive=True)
    assert res.items == 6  # 1 + 2 + 3, Kalender ausgelassen
    assert [(src, dest, n) for src, dest, n in res.folders] == [
        ("(start folder)", "T", 1), ("2019", "T/2019", 2), ("2019/Q1", "T/2019/Q1", 3)]
    assert {"T", "T/2019", "T/2019/Q1"} <= imap.folders
    assert "Folders: 3" in res.report(30, show_folders=True)


def test_bench_recursive_total_and_per_folder_limits():
    res = bench.run(archive_tree(), "Archiv:Inbox", limit=4, recursive=True)
    assert res.items == 4 and [n for _, _, n in res.folders] == [1, 2, 1]
    res = bench.run(archive_tree(), "Archiv:Inbox", limit=100, recursive=True, per_folder=1)
    assert res.items == 3 and [n for _, _, n in res.folders] == [1, 1, 1]


def test_bench_without_recursive_ignores_subfolders():
    res = bench.run(archive_tree(), "Archiv:Inbox", limit=100)
    assert res.items == 1 and len(res.folders) == 1
