# Account 1 - Google OAuth (Drive A/B/C) production checklist

Status: **owner action pending.** The engineering side is complete:
- device flow and web flow (PKCE + single-use state);
- per-connection vault entries;
- revocation on disconnect;
- reauth alerts.

Production OAuth readiness is **not claimed** until the owner has published the consent screen and reconnected.
Credentials are never sent in chat. The OAuth client JSON goes straight into the vault with
`python -m nexus.cloud.storage.setup_google <client.json> --delete-file` on the host.

## 1. Facts this checklist relies on (checked 2026-11-13)
- Refresh tokens expire in 7 days while the consent screen is "Testing". A Google Cloud project with an External consent
  screen in Testing status "is issued a refresh token expiring in 7 days", unless it requests only name, email and
  profile. `drive.file` is not in that exception.
  Source: https://developers.google.com/identity/protocols/oauth2 (Refresh token expiration).
- Other ways a refresh token stops working, from the same page:
  - the user revokes access;
  - the token is unused for six months;
  - a password change, for tokens with Gmail scopes (NEXUS requests none);
  - a limit of 100 refresh tokens per Google Account per client ID: older tokens are invalidated without warning.
- `drive.file` is a **non-sensitive** scope:
  https://developers.google.com/workspace/drive/api/guides/api-specific-auth.
  "If your app utilizes only non-sensitive scopes, it is not mandatory for your app to complete the app verification
  process" (https://support.google.com/cloud/answer/13463073).
- The device flow supports `drive.file` (allowed-scopes list,
  https://developers.google.com/identity/protocols/oauth2/limited-input-device).

## 2. Consent screen: production checklist (owner, Google Cloud Console, no billing)
- [ ] The project has **no billing account** attached. Drive API calls on `drive.file` are free.
- [ ] Drive API enabled.
- [ ] User type **External**. App name and support e-mail filled in. No logo needed: a logo triggers brand verification.
- [ ] Scopes exactly `https://www.googleapis.com/auth/drive.file`, `openid`, `email`. **Nothing else.** The full `drive`
      scope is restricted and is not used.
- [ ] Publishing status: **In production** ("Publish app"). Then reconnect A, B and C. Tokens issued under Testing keep
      their 7-day expiry.
- [ ] Optionally leave the app unverified. With non-sensitive scopes only, verification is not mandatory.
- [ ] Each of the three Google accounts is the owner's own account. Not a second account created to multiply quota.

## 3. Client type and redirect URIs
- Recommended client type: **TV and Limited Input devices** (device flow).
  - It needs no redirect URI, so it works from any host.
  - Connect with Studio → Drive & storage → Connect → google.com/device.
- Web client (optional): authorised redirect URI exactly `https://<NEXUS_PUBLIC_ORIGIN host>/api/v1/storage/oauth/callback`.
  - Plain `http` is refused except for `localhost` (`oauth.redirect_uri_ok`).
  - No wildcard and no other paths.
- The callback is the only unauthenticated storage route. It is protected by single-use state + PKCE.

## 4. Revocation
- Studio → Drive & storage → **Disconnect** (`POST /api/v1/storage/connections/{cid}/disconnect`):
  - revokes the grant at Google (`oauth2.googleapis.com/revoke`);
  - deletes the vault entry even if Google is unreachable;
  - marks the connection `disconnected`.
- From the Google side: myaccount.google.com → Security → Third-party connections → remove NEXUS. The next call
  raises a `reauth` alert.
- Suspected leak of the OAuth client secret: in the Cloud Console, reset the client secret, then store the new
  JSON with `setup_google`. Existing refresh tokens remain bound to the client ID.

## 5. Credential recovery
| Situation | Recovery |
|---|---|
| Testing-mode expiry or `invalid_grant` | `reauth:<cid>` alert → reconnect with a device code. Publish the consent screen (§2) to stop the 7-day expiry |
| Vault key lost | the encrypted entries are unreadable: reconnect A/B/C and re-store the OAuth client JSON (`ACCOUNT1_DISASTER_RECOVERY.md` §2) |
| Host rebuilt | restore the backup + inject the offline key: connections work without reconnecting (`vault.undecryptable == 0`) |
| Account no longer wanted | disconnect (§4); its files remain on that Drive; the artifact manifest records the location |

## 6. Real backup/restore validation against Drive (owner-authorised, not run by agents)
1. `NEXUS_BACKUP_PASSPHRASE` set. Destination = an authorised Drive connection with encryption.
2. Run `python -m nexus backup --to <drive destination>`, then `python -m nexus verify --to <drive destination>`.
3. Restore into a new folder (`ACCOUNT1_DISASTER_RECOVERY.md` §5). Expect `ok: true` and `vault.undecryptable: 0`.
4. Record the snapshot id, file count and result in `ACCOUNT1_ACCEPTANCE_RESULTS.md`.

## 7. Drive failover validation (owner-authorised)
1. In the storage rules, list A then B for one kind.
2. Disconnect A, or fill it to the reserve.
3. Upload one test file. It must land on B, with event `storage.uploaded` carrying `failover: true`.
4. Reconnect A.
5. Nothing is deleted at any point. Quota exhaustion is reported. It is never a reason to switch accounts.
