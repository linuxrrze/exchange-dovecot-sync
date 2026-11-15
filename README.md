# exchange-dovecot-sync (exsync)

Einweg-Sync von Exchange-Postfächern (Hauptpostfach **und** Archivpostfach) per EWS nach Dovecot,
mit optionaler Sieve-Filterung neuer Posteingangs-Mails.

**Stand: Prototyp.** Er beantwortet drei Fragen, bevor der eigentliche Sync gebaut wird:

1. Kommt der EWS-Zugriff auf Haupt- und Archivpostfach durch? → `exsync folders`
2. Wie schnell liefert EWS die Mails (Hochrechnung für den Erstabgleich)? → `exsync bench`
3. Greift Sieve bei LMTP-Zustellung auf dem (entfernten) Dovecot 2.4? → `exsync sieve-test`

## Einrichtung

```sh
uv sync
cp config.example.toml config.toml   # anpassen; config.toml ist in .gitignore
export EXSYNC_EWS_PASSWORD=...        # sonst wird abgefragt
export EXSYNC_IMAP_PASSWORD=...
```

## Prototyp-Befehle

```sh
uv run exsync folders                                  # Ordner + Anzahl Mails, Archiv als "Archiv:…"
uv run exsync bench Inbox -n 2000                      # nur EWS-Export messen
uv run exsync bench "Archiv:Inbox" -n 2000 --write     # zusätzlich per IMAP APPEND nach Dovecot
uv run exsync bench "Archiv:RRZE" -r --per-folder 50 --write   # mit Unterordnern
uv run exsync sieve-test                               # Testmail per LMTP, danach Zielordner suchen
```

`-n` begrenzt die Gesamtzahl geladener Mails (jeweils die neuesten). Mit `-r` werden auch die
Unterordner (nur Mailordner) übertragen; `-n` gilt dann über alle Ordner zusammen, `--per-folder`
begrenzt zusätzlich je Ordner. Die Ordner werden der Reihe nach abgearbeitet, ohne `--per-folder`
kann der erste große Ordner das Limit also allein aufbrauchen.

`bench --write` schreibt in den Dovecot-Ordner `exsync-bench` (änderbar mit `--target`), mit `-r`
samt Unterordnerstruktur darunter; danach löschen. Mehrfache Läufe erzeugen Duplikate. Die
Hochrechnung nimmt standardmäßig 30 GB an (`--total-gb`).

## Komplettkopie: `exsync copy`

```sh
uv run exsync -c config.toml copy --dry-run          # nur zählen, was kopiert würde
uv run exsync -c config.toml copy                    # Haupt- und Archivpostfach komplett kopieren
uv run exsync -c config.toml copy --exclude "Gelöschte Elemente" --exclude "Junk-E-Mail"
```

Kopiert alle Mailordner von Haupt- und Archivpostfach rekursiv nach Dovecot, mit derselben
Ordnerzuordnung wie `folders --target` (Posteingang → `INBOX`, Archiv unter `Archiv/`).
Kalender, Kontakte und Aufgaben werden ausgelassen.

**Abbruch und Fortsetzen:** Einfach denselben Befehl erneut aufrufen; übertragen werden nur noch
fehlende Mails. Eine Mail gilt als vorhanden, wenn

1. ihre EWS-ItemId in der Zustandsdatei steht (Standard
   `~/.local/state/exsync/<postfach>__<dovecot-user>@<host>.sqlite3`, änderbar mit `--state`);
   sie wird direkt nach jedem erfolgreichen Schreiben aktualisiert, oder
2. ihre Message-ID schon im Zielordner liegt. Das greift auch ohne Zustandsdatei.

Pro Ordner holt exsync zuerst nur die Liste der Mails und lädt den Inhalt nur für fehlende Mails.
Grenzen: Wird genau während des Schreibens einer Mail **ohne** Message-ID abgebrochen, kann diese
eine Mail doppelt ankommen. Mehrere Mails mit gleicher Message-ID im selben Ordner werden nur einmal
übertragen. Verschiebt man Mails in Exchange zwischen zwei Läufen, landen sie zusätzlich im neuen
Zielordner (das ist kein Sync, Löschungen und Verschiebungen werden nicht nachgezogen).

## Passwörter

Reihenfolge: Umgebungsvariable (`EXSYNC_EWS_PASSWORD`, `EXSYNC_IMAP_PASSWORD`), dann `password` im
jeweiligen Abschnitt der `config.toml`, sonst Abfrage. Wer Passwörter in die Datei schreibt, sollte
sie mit `chmod 600 config.toml` schützen; exsync warnt, wenn sie für andere lesbar ist.

## Voraussetzungen auf Dovecot-Seite

- **IMAP:** Login als Benutzer oder per Master-User (`master_user` in der Konfiguration →
  Login `benutzer*master`).
- **LMTP über TCP** (für `sieve-test` und später für neue Posteingangs-Mails), Dovecot 2.4:

  ```
  service lmtp {
    inet_listener lmtp {
      port = 24
    }
  }
  protocol lmtp {
    mail_plugins {
      sieve = yes
    }
  }
  ```

  Den Port per Firewall nur für den Sync-Rechner freigeben, LMTP hat keine Authentifizierung.

## Geplante Arbeitsweise des Syncs („Regel 2“)

- Übertragen werden nur **Änderungen in Exchange** (`SyncFolderHierarchy`/`SyncFolderItems` mit
  gespeichertem sync_state je Ordner), stündlich per systemd-Timer.
- Lokale Änderungen in Dovecot (Sieve, Verschieben, Löschen) bleiben erhalten. Fehlende Mails werden
  nicht wiederhergestellt; Status-Änderungen aus Exchange werden an der Mail nachgezogen, wo sie gerade liegt.
- Hauptpostfach 1:1, Archivpostfach unter `Archiv/`. Nur Mailordner (`IPF.Note`), keine Kalender/Kontakte.
- Neue Mails im Exchange-Posteingang gehen per LMTP (Sieve läuft), alles andere per IMAP APPEND.
