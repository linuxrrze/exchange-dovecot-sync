# exchange-dovecot-sync (exsync)

One-way sync of Exchange mailboxes (primary mailbox **and** archive mailbox) via EWS to Dovecot,
with optional Sieve filtering of new inbox mails.

**Status: prototype.** It currently answers three questions before the actual sync is built and
already provides a resumable full copy:

1. Does EWS access to the primary and archive mailbox work? → `exsync folders`
2. How fast does EWS deliver mails (extrapolation for the initial copy)? → `exsync bench`
3. Does Sieve run on LMTP delivery to the (remote) Dovecot 2.4? → `exsync sieve-test`

## Setup

```sh
uv sync
cp config.example.toml config.toml   # adjust; config.toml is in .gitignore
chmod 600 config.toml                # if you put passwords into it
```

## Prototype commands

```sh
uv run exsync folders                                   # folders + mail counts, archive as "Archive:…"
uv run exsync folders --target                          # … plus Dovecot target folder and status
uv run exsync bench Inbox -n 2000                       # measure EWS export only
uv run exsync bench "Archive:Inbox" -n 2000 --write     # additionally IMAP APPEND to Dovecot
uv run exsync bench "Archive:Projects" -r --per-folder 50 --write   # including subfolders
uv run exsync sieve-test                                # test mail via LMTP, then find its folder
```

Use `-c PATH` (before the command) to select a configuration file; the default is `config.toml`
in the current directory. Archive folders are addressed with the prefix `Archive:` (`Archiv:` is
accepted as well).

`folders --target` shows the Dovecot folder for each Exchange folder and marks it as `exists` or
`new`; `duplicate` means that several Exchange folders would end up in the same Dovecot folder
(e.g. because a folder name contains the Dovecot hierarchy delimiter). `--offline` skips the
connection to Dovecot.

`bench`: `-n` caps the total number of fetched mails (the newest each). With `-r` subfolders (mail
folders only) are included; `-n` then applies across all folders and `--per-folder` additionally
per folder. Folders are processed one after another, so without `--per-folder` a large first
folder can use up the whole limit. `bench --write` writes to the Dovecot folder `exsync-bench`
(change with `--target`), with `-r` including the subfolder structure below it; delete it
afterwards. Repeated runs create duplicates. The extrapolation assumes 30 GB by default
(`--total-gb`).

## Full copy: `exsync copy`

```sh
uv run exsync copy --dry-run          # only count what would be copied
uv run exsync copy                    # copy primary and archive mailbox completely
uv run exsync copy --exclude "Deleted Items" --exclude "Junk Email"
```

Copies all mail folders of the primary and the archive mailbox recursively to Dovecot, with the
same folder mapping as `folders --target`: the inbox becomes `INBOX`, the archive mailbox goes
below `Archiv/` (configurable via `archive_prefix`). Calendar items, contacts and tasks are
skipped, also when they are stored in mail folders (e.g. deleted ones in "Deleted Items");
meeting requests and responses are copied. `--no-archive` skips the archive mailbox,
`--exclude` skips a folder including its subfolders (path as shown by `folders`, repeatable).

**Progress:** in a terminal a single line is updated in place (listing, comparing, copying with
rate and estimated time left) and replaced by a summary line per folder. When the output is
redirected, at most one progress line is written every 30 seconds.

**Interrupting and resuming:** just run the same command again; only missing mails are
transferred. A mail counts as present if

1. its EWS ItemId is in the state file (default
   `~/.local/state/exsync/<mailbox>__<dovecot-user>@<host>.sqlite3`, change with `--state`),
   which is updated right after every successful write, or
2. its Message-ID is already in the target folder. This also works without a state file.

**Lost IMAP connection:** exsync reconnects automatically (after 5, 15 and 60 s) and writes the
mail again; if the mail has a Message-ID it first checks whether the interrupted APPEND did arrive,
so no duplicate is created. If a mail still cannot be written it is counted as an error and
retried on the next run; after three such mails in a row (Dovecot unreachable) the copy stops.

**Mails Exchange cannot export** ("MIME content conversion failed", e.g. damaged items from old
migrations or PST imports): exsync rebuilds the mail from its EWS properties — addresses, subject,
date, Message-ID, body, attachments (inline images, attached mails) and, if Exchange still has
them, the original headers such as `Received`. If single properties are unreadable as well, the
mail is built without them. Rebuilt mails carry the header `X-Exsync-Reconstructed: yes; <reason>`
and are listed during the run. Lost are details that only exist in the original MIME (exact
structure, S/MIME signatures); IRM-protected mails stay unreadable.

If even the rebuild fails, the mail is reported with date, sender and subject, recorded in the
state file and skipped from then on; the summary lists all such mails. Later:

```sh
uv run exsync failed            # list them (from the state file)
uv run exsync failed --retry    # forget them, the next copy run tries them again
```

**Exchange throttling:** if Exchange answers "server busy" (`ErrorServerBusy`), exsync waits as
long as the server asks (a warning "Server requested back off …" is shown) and retries the same
request; temporary connection errors are retried as well. A single wait longer than `max_wait`
(`[ews]`, default 3600 s) aborts the run, which can then be resumed.

For each folder exsync first fetches only the list of mails (1000 per request) and downloads the
content only for missing mails.

Limitations: if the run is interrupted exactly while a mail **without** a Message-ID is being
written, that one mail may arrive twice. Several mails with the same Message-ID in the same folder
are transferred only once. This is not a sync: mails moved or deleted in Exchange between two runs
are not moved or deleted in Dovecot (moved mails additionally appear in the new target folder).

## Passwords

Order: environment variable (`EXSYNC_EWS_PASSWORD`, `EXSYNC_IMAP_PASSWORD`), then `password` in the
respective section of `config.toml`, otherwise an interactive prompt. If you store passwords in the
file, protect it with `chmod 600 config.toml`; exsync warns if it is readable by others.

## Requirements on the Dovecot side

- **IMAP:** login as the user or via a master user (`master_user` in the configuration → login
  `user*master`).
- **LMTP over TCP** (for `sieve-test` and later for new inbox mails), Dovecot 2.4:

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

  Allow the port only for the sync host in the firewall; LMTP has no authentication.

## Planned sync behaviour

- Only **changes in Exchange** are transferred (`SyncFolderHierarchy`/`SyncFolderItems` with a
  stored sync_state per folder), hourly via a systemd timer.
- Local changes in Dovecot (Sieve, moving, deleting) are kept. Missing mails are not restored;
  state changes from Exchange are applied to the mail wherever it currently is.
- Primary mailbox 1:1, archive mailbox below `Archiv/`. Mail folders only (`IPF.Note`), no
  calendars/contacts.
- New mails in the Exchange inbox are delivered via LMTP (Sieve runs), everything else via IMAP
  APPEND.
