"""Konfiguration aus einer TOML-Datei.

Passwörter: Umgebungsvariable (EXSYNC_EWS_PASSWORD / EXSYNC_IMAP_PASSWORD) hat Vorrang, dann
`password` in der Konfigurationsdatei, sonst wird interaktiv abgefragt.
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
    server: str  # Hostname oder vollständige EWS-URL
    email: str  # primäre SMTP-Adresse des Postfachs
    username: str  # z.B. DOMÄNE\benutzer
    auth_type: Literal["NTLM", "basic"] = "NTLM"
    verify_tls: bool = True
    password: str | None = field(default=None, repr=False)

    def get_password(self) -> str:
        return _secret("EXSYNC_EWS_PASSWORD", self.password, f"EWS-Passwort für {self.username}: ")


@dataclass
class DovecotConfig:
    user: str  # Dovecot-Benutzer, in dessen Postfach geschrieben wird
    imap_host: str = "localhost"
    imap_port: int = 993
    ssl: bool = True
    verify_tls: bool = True
    master_user: str | None = None  # Login als "<user>*<master_user>"
    archive_prefix: str = "Archiv"  # Zielordner für das Archivpostfach
    lmtp_host: str | None = None  # für Sieve: Zustellung neuer Posteingangs-Mails per LMTP
    lmtp_port: int = 24
    password: str | None = field(default=None, repr=False)  # des Benutzers bzw. Master-Users

    @property
    def login(self) -> str:
        return f"{self.user}*{self.master_user}" if self.master_user else self.user

    def get_password(self) -> str:
        who = self.master_user or self.user
        return _secret("EXSYNC_IMAP_PASSWORD", self.password, f"IMAP-Passwort für {who}: ")


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
    """Warnt, wenn eine Datei mit Passwörtern für Gruppe oder andere lesbar ist."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(f"Warnung: {path} enthält Passwörter und ist für andere lesbar. "
              f"Abhilfe: chmod 600 {path}", file=sys.stderr)
        return True
    return False


def _secret(env: str, configured: str | None, prompt: str) -> str:
    value = os.environ.get(env)
    if value:
        return value
    if configured:
        return configured
    return getpass.getpass(prompt)
