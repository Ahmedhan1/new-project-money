# Backup & Recovery

Everything below works from the launchers (Windows) or `python -m nexus …` (any OS).

## What can be lost, and how it comes back
| Lost | Recover with | Time |
|---|---|---|
| The chat/agent session | `NEXUS_RESUME` → read `NEXUS_HANDOFF.md` | minutes |
| The code checkout | `git clone` → `NEXUS_SETUP` | setup time |
| A crashed worker / reboot mid-render | `NEXUS_START` — jobs resume from their last finished stage | automatic |
| The data folder (`.nexus`) or the whole machine | `NEXUS_RESTORE` from any destination | size-dependent |
| One backup destination | nothing to do — the others are independent; fix it and run `NEXUS_BACKUP` | — |
| Model weights | `python -m nexus models fetch` (never backed up; resumable download) | ~7 GB download |

## Back up
`NEXUS_BACKUP.bat` = `python -m nexus backup --drill`
1. Takes a consistent snapshot of the live database (SQLite online-backup API; safe while jobs run).
2. Uploads only objects the destination does not already hold (content-addressed → incremental + deduplicated).
3. **Verifies**: every object is re-read from the destination; size must match, and SHA-256 is compared for everything uploaded in this run. Only then is the backup reported `OK`.
4. `--drill`: restores the snapshot into a scratch folder, checks every file's checksum and every media/timeline reference, deletes the scratch folder. Proves the backup is actually restorable.

Other commands: `nexus verify [--to NAME]` (deep check: every checksum) · `nexus storage` (destinations, usage, caps) ·
`nexus backup --tiers state,metadata` · `nexus backup --to usb`.

Result files: `NEXUS_HOME/last_backup.json`, `last_restore_drill.json` (shown by `nexus status` and the UI).

## Restore
`NEXUS_RESTORE.bat` = `python -m nexus restore <new-folder> [--from NAME] [--snapshot ID]`
- Always writes into a **new, empty folder**; refuses a non-empty target and refuses the live data folder.
- Each file is checksum-verified as written; a corrupt object is reported and not restored as good.
- Absolute paths stored in projects are rewritten to the new location; then every project is opened and its media,
  timelines, narration and music references are checked (`references.missing` must be empty).
- To switch over: stop NEXUS → rename `.nexus` to `.nexus-old` → rename the restored folder to `.nexus` → start.
  Delete `.nexus-old` only after you have opened your projects.

## Full disaster recovery (new machine)
1. `git clone <repo>` → double-click `NEXUS_SETUP.bat` (Linux: `scripts/setup.sh`).
2. For a remote destination: install rclone, recreate the remote with `rclone config`, copy your `storage.json` entry
   into `.nexus/storage.json`, set `NEXUS_BACKUP_PASSPHRASE`.
3. `python -m nexus restore .nexus-restored --from <name>` → switch over as above.
4. `NEXUS_DOCTOR` → `NEXUS_START`.

## Encryption
AES-256-GCM, key derived from `NEXUS_BACKUP_PASSPHRASE` with scrypt; chunk order and truncation are authenticated.
Mandatory for remote destinations; optional for local ones (`"encrypt": true`). The snapshot manifest (file names) is
encrypted too. **There is no passphrase recovery.**

## Known limits
- A backup is queued automatically every 24 h while the app runs (`"backup_every_hours"` in `settings.json`, 0 = off). Old snapshots are not pruned automatically yet (`nexus` has `backup.prune`; task STO-4).
- Content hashes are visible to the storage provider as object names (they reveal nothing but equality of files).
- No remote destination has been exercised against a real cloud account yet (task STO-3 — needs the user's authorisation);
  the rclone backend is tested against rclone's local-path remote.
