# Account 1 setup guide (owner)

Nothing here needs a payment card. Every step is something only you, the owner, can do; NEXUS never creates
accounts or signs in on your behalf.

## 1. Run the control plane
Development (this cloud session or a Codespace): `python -m uvicorn nexus.api:app --host 127.0.0.1 --port 8765` and open
`http://127.0.0.1:8765/studio`. Keep forwarded ports **Private**.
Any other host: set `NEXUS_API_TOKEN` (a long random string, from the host's secret store) and serve over HTTPS; the
Studio asks for the token once and keeps only a signed HttpOnly cookie. Choosing a permanent host is your decision:
`docs/ACCOUNT1_PROVIDER_COMPARISON.md` lists free options and their limits; nothing is deployed publicly without you.

## 2. Kaggle (one account)
1. Kaggle → Settings → API → **Expire** the tokens that were pasted into chat, then **Create New Token**.
2. Store it in the vault (never in Git, a notebook or chat):
   `python -c "from nexus import vault; import getpass; vault.put('kaggle_primary', getpass.getpass('Kaggle token: '), {'provider':'kaggle'})"`
3. Studio → Compute providers → **Verify** (runs a ~2 min private probe kernel on the GPU).
NEXUS uses exactly one Kaggle account. When its quota is used up, GPU work waits (or CPU work runs locally); it never
switches to another Kaggle account. GPU phone verification on Kaggle is required for GPU + internet.

## 3. Google Drive A / B / C
1. Google Cloud Console (any of your Google accounts, no billing) → create a project → APIs & Services → enable
   **Google Drive API** → OAuth consent screen (External; add your three accounts as test users, then **Publish to
   production** - in Testing mode refresh tokens expire after 7 days) → Credentials → **Create OAuth client** →
   type **TV and Limited Input devices** → download the JSON.
2. `python -m nexus.cloud.storage.setup_google path/to/client.json --delete-file` (stores it in the vault).
3. Studio → Drive & storage → **Connect** Drive A: open google.com/device, enter the code, sign in with account A,
   allow access. Repeat for B and C with the other accounts. Scope: `drive.file` (only files NEXUS creates or you open
   with it).
4. Studio → Drive & storage → storage rules: list the connections NEXUS may upload to (`authorized_remote`). A
   connection alone does not authorise uploads.
5. Live check: `NEXUS_LIVE_DRIVE=1 python -m pytest -q tests/live/test_drive_live.py`.
Three accounts are three separate 15 GB allowances; NEXUS never rotates accounts to get around a provider's limits.

## 4. Your own GPU PC or a Colab session (optional fallback compute)
Studio → Compute → register a worker → copy the one-time worker token → on the GPU PC run
`python nexus/cloud/worker_kit/pull_worker.py --api https://<your-host> --kind user_gpu` (it asks for the worker token, `<worker_id>.<token>`, or reads `NEXUS_WORKER_TOKEN`), or open
the generated Colab notebook (`GET /api/v1/compute/workers/{id}/notebook`) in your own Colab account.

## 5. Back up the vault key
`NEXUS_HOME/secrets/vault.key` decrypts every stored token. Back it up to a password manager (or inject it as
`NEXUS_VAULT_KEY`). Without it the secrets fail closed and must be re-entered.
