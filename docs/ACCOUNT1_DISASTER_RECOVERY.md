# Account 1 - vault backup and disaster recovery

Applies to the control plane's own data. Media masters on Drive A/B/C have their own copies, rules and checksums
(`ACCOUNT1_CLOUD_ARCHITECTURE.md`).

A recovery drill never overwrites or deletes owner data:
- `restore` refuses the live `NEXUS_HOME` and any non-empty folder;
- the drill restores into a scratch folder and deletes only that folder.

## 1. What is protected, and where its copy lives
| Data | Location | In backups | How it is protected |
|---|---|---|---|
| Database: jobs, remote jobs, checkpoints, artifacts, storage manifests, connection records, scheduler, audit, events | `NEXUS_HOME/nexus.db` | yes, as a consistent SQLite online-backup snapshot | checksummed; encrypted for remote destinations |
| Project metadata: timelines, reports | `NEXUS_HOME/projects/*/timelines,reports` | yes, tier `metadata` | same |
| Settings and storage rules | `settings.json`, `storage.json` | yes | same |
| Vault entries: Drive/Kaggle credentials, OAuth client, worker-token hashes | `NEXUS_HOME/secrets/*.secret` | **yes, as Fernet ciphertext** | already encrypted at rest; useless without the key |
| **Vault key** | `NEXUS_VAULT_KEY` (production) or `secrets/vault.key` (dev) | **never** | the owner keeps an offline copy (§2) |
| Backup passphrase | `NEXUS_BACKUP_PASSPHRASE` | never | the owner keeps an offline copy |
| Owner token | `NEXUS_API_TOKEN` | never | can be regenerated at any time; ends all sessions |
| Artifact files on Drive | Drive A/B/C | n/a (they are the remote copies) | sha256 manifest in the database; re-verified on read |

Why the key is never in a backup: the backup holds ciphertext of every credential. A backup that also held the key
would let anyone with that one backup decrypt them all.
- `backup.collect()` never adds `vault.key`.
- `restore()` flags a snapshot that contains one.
- `test_backup_carries_vault_ciphertext_never_the_key` checks the raw bytes at the destination for both the
  credential and the key.

## 2. Offline vault-key backup (owner runbook)
Do this once now, and again whenever the key changes. The key is never printed on screen.

1. Plug in a removable drive, or choose a password manager's import folder. It must not be inside `NEXUS_HOME`, the
   repository, or any backup destination; the tool refuses those paths.
2. `python -m nexus vault-key export --to E:\nexus-vault.key` (Windows) or `--to /media/usb/nexus-vault.key`.
   - The command writes the file with mode 0600 and never overwrites an existing file.
   - It prints only the **fingerprint** (`sha256:xxxxxxxxxxxxxxxx`).
   - The export is recorded in the audit log, with the fingerprint only.
3. Write the fingerprint on the drive's label or in the password-manager entry.
4. Check the copy: `python -m nexus vault-key verify --from-file E:\nexus-vault.key` must report
   `"matches_active_key": true`.
5. Keep two copies in two places, for example a password manager plus a USB drive in a drawer.
   Never put them in Git, chat, e-mail, Drive A/B/C or a Kaggle dataset.
6. Production hosts:
   - Put the key into the host's secret store as `NEXUS_VAULT_KEY`.
   - Delete `NEXUS_HOME/secrets/vault.key` only after step 4 passed. The `vault_key_offdisk` gate requires it.
7. Keep the backup passphrase (`NEXUS_BACKUP_PASSPHRASE`) offline in the same way. Without it, remote backups cannot
   be decrypted.

If the key is lost:
- The vault fails closed. `/api/v1/health` shows `vault.undecryptable > 0`, and the `vault_readable` gate fails.
- Recovery means reconnecting each Drive account (device code), re-storing the Kaggle token on the host, and
  re-issuing worker tokens.
- No data or media is lost. Only credentials must be re-entered.

## 3. Backups
- `python -m nexus backup` copies to every enabled destination.
  - Each destination succeeds or fails on its own.
  - The run counts as successful only after every object has been re-read and its sha256 matches.
  - Remote destinations require `NEXUS_BACKUP_PASSPHRASE` (AES-256-GCM, scrypt-derived key).
- Production requires at least one destination outside `NEXUS_HOME` (`backup_offsite` gate).
  - Options: a second disk, or an authorised Drive connection with encryption.
  - Same-disk-only backups are refused.
- History:
  - `NEXUS_HOME/last_backup.json` holds the last run.
  - The `backup` documents in the database hold every run (`python -m nexus status` shows the last backup and the last
    restore drill).
- Alerts:
  - `backup:failed` when no destination holds the backup or some failed;
  - `backup:stale` when the last backup is older than twice `backup_every_hours`;
  - `backup:none` in production.

## 4. Verify
`python -m nexus verify --to <destination>` re-reads every blob of the latest snapshot and compares sizes and
checksums. Corruption and missing objects are reported (`test_corruption_and_missing_objects_are_detected`).

## 5. Restore and recovery validation
1. Choose a new, empty folder: `python -m nexus restore D:\nexus-restored --from <destination>`.
2. Each file is checksum-verified as it is written. Paths are rewritten to the new home. Every project reference is
   opened and checked.
3. The result reports `vault: {secrets, key_available, decryptable, undecryptable}`:
   - Run with `NEXUS_VAULT_KEY` set to the offline key: every credential must decrypt (`undecryptable: 0`).
   - A wrong key is reported (`undecryptable > 0`) and nothing is overwritten.
4. Switch over:
   - stop NEXUS;
   - point `NEXUS_HOME` to the restored folder, or move it into place after checking it;
   - set `NEXUS_VAULT_KEY`;
   - start;
   - confirm `production-check` passes and `/api/v1/health/ready` returns 200.
5. Drill without switching: `python -m nexus backup --drill` restores into a scratch folder, verifies it, deletes the
   scratch folder, and records `last_restore_drill.json`.

Tested (fast suite, Linux + Windows CI) in `test_storage_backup.py` (9 tests):
- verify, restore, reopen a project;
- corruption detection;
- one destination down;
- free-allowance cap;
- encryption and wrong passphrase;
- prune.

Also tested, in `test_production.py`:
- vault ciphertext is backed up and the key never is;
- restore with no key, the right key and a wrong key;
- restore never overwrites existing data.

## 6. Drive failover and real external restoration
- Upload failover is automatic but bounded by the owner's rules:
  - it only uses destinations listed for that kind of file;
  - it checks provider-reported free space;
  - it never rotates accounts to get around a quota.
- A real restore from Drive, a real revoke and a real failover drill against the owner's accounts are **owner-authorised
  steps**. They are not run by agents:
  1. Back up to the Drive destination: `NEXUS_BACKUP_PASSPHRASE` must be set.
  2. Verify it.
  3. Restore into a new folder. Never over the live data.
  4. Confirm `vault.undecryptable == 0` with the offline key.
  5. Record the result in `docs/ACCOUNT1_ACCEPTANCE_RESULTS.md`.

## 7. Service recovery (operator cheat-sheet)
| Symptom | First step |
|---|---|
| `/health/ready` 503 | `python -m nexus production-check` names the failing gate and its fix |
| `vault_readable` fails | wrong `NEXUS_VAULT_KEY`: compare with `vault-key verify --from-file <offline copy>` |
| `single_instance` fails | another API process on this `NEXUS_HOME`: `python -m nexus stop`, or check `run/api.lock.info` for the pid |
| Database will not open | stop; restore the last verified snapshot into a new folder (§5); switch over |
| Unclean-restart notification | read `logs/api.log` (or `journalctl -u nexus`); jobs resume from their checkpoints |
| Drive `reauth` alert | reconnect with the device code. If OAuth is still in Testing mode, refresh tokens expire after 7 days: see the OAuth checklist |
| Disk low | free space; old exports can be moved to a storage destination |
