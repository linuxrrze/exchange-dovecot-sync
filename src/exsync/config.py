"""Configuration from a TOML file.

Passwords: the environment variable (EXSYNC_EWS_PASSWORD / EXSYNC_IMAP_PASSWORD) takes
precedence, then `password` in the configuration file, otherwise an interactive prompt.
"""

from __future__ import annotations

import getpass
import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class EwsConfig:
    server: str  # host name or full EWS URL
    email: str  # primary SMTP address of the mailbox
    username: str  # e.g. DOMAIN\user
    auth_type: Literal["NTLM", "basic"] = "NTLM"
    verify_tls: bool = True
    max_wait: int = 3600  # longest wait (s) accepted when the server is busy or unreachable
    password: str | None = field(default=None, repr=False)

    def get_password(self) -> str:
        return _secret("EXSYNC_EWS_PASSWORD", self.password, f"EWS password for {self.username}: ")


@dataclass
class DovecotConfig:
    user: str  # Dovecot user whose mailbox is written to
    imap_host: str = "localhost"
    imap_port: int = 993
    ssl: bool = True
    verify_tls: bool = True
    master_user: str | None = None  # login as "<user>*<master_user>"
    archive_prefix: str = "Archiv"  # target folder for the archive mailbox
    lmtp_host: str | None = None  # for Sieve: deliver new inbox mails via LMTP
    lmtp_port: int = 24
    password: str | None = field(default=None, repr=False)  # of the user or the master user

    @property
    def login(self) -> str:
        return f"{self.user}*{self.master_user}" if self.master_user else self.user

    def get_password(self) -> str:
        who = self.master_user or self.user
        return _secret("EXSYNC_IMAP_PASSWORD", self.password, f"IMAP password for {who}: ")


@dataclass
class Config:
    ews: EwsConfig
    dovecot: DovecotConfig | None = None
    extra: dict = field(default_factory=dict)
    path: Path | None = None


def load(path: str | Path) -> Config:
    path = Path(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    ews = EwsConfig(**data.pop("ews"))
    dc = data.pop("dovecot", None)
    cfg = Config(ews=ews, dovecot=DovecotConfig(**dc) if dc else None, extra=data, path=path)
    if ews.password or (cfg.dovecot and cfg.dovecot.password):
        warn_if_readable(path)
    return cfg


def warn_if_readable(path: Path) -> bool:
    """Warn if a file containing passwords is readable by group or others."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(f"Warning: {path} contains passwords and is readable by others. "
              f"Fix: chmod 600 {path}", file=sys.stderr)
        return True
    return False


def _secret(env: str, configured: str | None, prompt: str) -> str:
    value = os.environ.get(env)
    if value:
        return value
    if configured:
        return configured
    return getpass.getpass(prompt)
