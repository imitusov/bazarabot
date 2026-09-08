# Proposal: #25 backups off the database volume, plus a restore check

**Kind:** spec + deploy. `ops.backup` already uses SQLite `backup()` and does
not raise. `DB_PATH` and `BACKUP_DIR` default onto the same `/data` mount.
A volume loss takes the live DB and every local copy. Brief §11: the local
database is the system of record for strategy attribution; the broker will
not reconstruct it.

**Should say:**

1. Local `run()` stays the consistent snapshot. After it succeeds, an
   **optional** offsite step (object storage or a second mount). Missing
   offsite config is a configuration error in live, or a documented "local
   only" mode — pick one in the spec; do not silently skip upload.
2. Upload / verify failure: alert, **do not** stop trading (same as local
   `backup_failed`).
3. Before a backup counts as `backup_ok`: open the file, cheap integrity
   (`PRAGMA integrity_check` plus a row count on a named table). A file
   `prune` would keep that cannot be opened is not a backup.
4. Restore procedure in `environment-setup.md`, tested once. Untested restore
   is not a backup.
5. Credentials only through `config`. Never log them.

**Do not:** add an S3 SDK until it is in the spec's dependency list. Do not
change `_BACKUP_RETENTION_DAYS` in the same amendment as offsite unless the
brief wants a weekly long-copy.

**§3.2:**

- Integrity failure → `backup_failed`, dest still returned, trading continues.
- Upload failure (once specified) → alert, same.
- Local copy still on `BACKUP_DIR` even when offsite is configured.

**Modules:** `01-config` if new variables, `31-ops-backup`, deploy compose /
`environment-setup.md`. Human reads offsite vs second volume (cost, secrets).

**Stop:** do not invent a bucket name or IAM shape in code first.
