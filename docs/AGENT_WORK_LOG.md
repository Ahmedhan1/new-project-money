# Agent work log

Newest session first. Facts only: what was verified, what was changed, what is open.

## Session 4 — 2026-11-12 — takeover and parallel build

- **Branch** `claude/hopeful-allen-luubcy`, started from `main` @ `ec5af11`, fast-forwarded to `42f02b5`
  (recovered from `origin/claude/optimistic-franklin-376ndo`, PR #2).
- **Baseline**: ruff clean; `pytest tests/fast` → 63 passed, 1 skipped; `nexus doctor` → FFmpeg OK, no ML models, no GPU.
- **Recovery report + gap analysis + contracts**: `docs/MASTER_ROADMAP.md`.
- **Agents**: eight workstreams A–H (table in the roadmap), each in an isolated Git worktree, model Opus.
  Results are recorded below as they are merged.

### Workstream results


### Workstream results (session 4)
All eight Opus agents stopped at 07:3x UTC on the account's 5-hour usage limit, before finishing. Their partial work was
committed on their worktree branches (`worktree-agent-*`, local only) and finished, tested and merged by the lead:

| WS | Agent left | Lead finished | Merge |
|---|---|---|---|
| A renderer/VFX | `fx/post.py`, `fx/titles.py` (builders) | wired effects/xfade/captions/titles/PiP/overlays/correction/stabilise into `render.py`, parallel + draft, guards, 13 tests, `docs/TIMELINE.md` | `ee959d5` |
| B director | `director.py`, `media/understand.py`, `direction.yaml` (1 syntax error) | fixed; cross-segment sentences, hook substance/interviewer caps, no sign-off endings, relative topic threshold; 16 tests | `512bc99` |
| C shorts v2 | `select/factory/context` code | missing growth helpers, platform mapping, shared sentences, titles from the clip's own lines; 10 tests | `c6165aa` |
| D languages | `languages.yaml`, `speech/packs.py` | dialect adaptation, CJK segmentation, voice licence gate, fonts, pack QA, localize wiring; 16 tests | `d189f7c` |
| E media engine | `encoders.py`, `export.py`, `proxy.py`, `ff.py` | renderer integration (CPU settings kept), 9 tests | `3d3ddf0` |
| F platforms | `platforms.yaml` (researched) | `platforms.py`, `publish.deliver`, deliver job/API/UI, 9 tests + API test | `ca44e88` |
| G quality | `quality.py` | lint (bidi chars), 10 tests, integration as a render-job stage, caption layout fix | `88c21ad` |
| H generation | `generation.yaml` + a real CPU benchmark | `nexus/generate` (probe, worker, providers, label, claim guard), 6 tests, `docs/GENERATION.md` | `15fcdb0` |

### Integration (lead)
- INT-4: director/quality/plan/EDL/draft API + UI; shorts v2 job/API/UI; destination selector (deliver job); rights API.
- Demos on the NASA interview: 1 (edit), 2 (shorts), 3 (8 languages), 4 (generated stills), 5 (SIGKILL recovery). Evidence:
  `docs/results/demos/`. Defects the demos exposed and that were fixed: pleasantry/interviewer hooks, fragment sentences,
  sign-off endings, level jumps at cuts, false localization failure, captions over the face / outside the Shorts safe zone,
  quality checker's caption model, three German MT errors, Chinese mid-word caption breaks.
- Open follow-ups registered: FX-2, BRL-1, DIA-1, AUD-2, LANG-2 (+ WIN-1, GEN-2, PUB-1, ANA-1, STO-3 blocked on the owner).

## Session 5 (2026-11-12) — final quality gap closure

Agents: the second batch of seven parallel agents all stopped at the account usage limit within ~10 minutes with no
output, so the lead did the work sequentially. Every item below was driven by a defect found on the real NASA interview.

| Area | What changed | Found by | Commit |
|---|---|---|---|
| Renderer FX-2 | freeze, split screen, chroma key, masks, credits roll, keyframes (pos/scale/volume), J/L-cut audio; 20 pixel/audio tests | gap list | `7e02171`, `8943723` |
| B-roll BRL-1 | NASA Image & Video Library + Openverse, licence/credit/provenance, subject gate, phash dedupe, Ken Burns | single-camera repetition | `34ebbe2`, `519b90d` |
| B-roll | footage in-point from a motion profile; only that window fetched at 1280x720 | QA: 2.3 s frozen launch pad; 320x180 upscale | `c9dfcf7` |
| Audio AUD-2 | RNNoise + gate before the leveller | noise floor on every render | `fbd7689` |
| Diarisation DIA-1 | WeSpeaker ONNX + clustering, roles, manual correction; split segments where the voice changes | merged Q+A segments | `9a11fd5`, `bd690b8` |
| Faces | YuNet face track; shorts and platform delivery crop on it | blur-fill on 9:16 delivery | `6c57574` |
| Director | lower thirds only from names a person set | "guest"/"interviewer" on screen | `6c57574` |
| Renderer | segment audio held to its picture in the join | QA: audio 0.3 s longer than video | `aaf6412` |
| Jobs | dead local worker recovered at once (also zombie); recovery on a watchdog thread | killed render stuck 'running' after fast restart | `d0349b8`, `ee6f4cc` |
| Shorts | ≤ 5 hashtags from entities/repeated words; never open on the interviewer | 39 tags incl. #Absolutely | `0f1a991`, `1acf0c0` |
| Localize | units = words heard in the clip, whole sentences ≤ 28 words, short output retranslated | MT inventing endings for fragments | `fb243a4` |
| Delivery | export verify + size-cap re-encode inside the deliver job; subtitle package with readiness manifest | gap list | `628ca30`, `dc40e8d` |
| Windows | `windows-e2e.yml`: SETUP → START → API e2e (non-ASCII paths) → STATUS → BACKUP drill → STOP on a clean VM | WIN-1 | `33ac0af`, `3cb50aa` |
| Release | `scripts/release_check.py`: fresh clone, hygiene, ruff, fast suite | release discipline | `1ed89a0` |


## Account 3 session (2026-11-12) — intelligence, research, strategy, languages, publishing, growth

Lead built contracts v1 + model router + API/UI mounts first, then six parallel sub-agents in worktrees (A creators,
B models, C market, D language, E publishing, F growth). All six finished with tests and evidence; the lead reviewed
scope, ran tests, merged each with `--no-ff` and applied integration requests.

| Area | What changed | Found by | Commit |
|---|---|---|---|
| Foundation | contracts v1, model router, adapters, cost-guard free-tier exception | brief | `bd71690` |
| UI/API | `/api/a3/*`, `intelligence.js` (7 pages), job kinds a3-market-research / a3-reference / a3-strategy | brief | `6a4b060`, `7ff7818` |
| Strategy | originality + grounding gates, hand-off to Account 2 create job | brief | `4165931` |
| Streams A-F | merged | sub-agents | `6560ba6`, `68365d6`, `3917c9d`, `74fb0d2`, `bee4233`, merge of E |
| Jobs | heavy_lock re-entrant per thread (worker deadlocked on itself during a real research job) | py-spy on the real job | this session |
| Jobs | light (network-bound) job kinds skip the CPU lock | stream E | `294206c` |
| Router | soft quality bar with labelled drafts; strict mode | stream B measurement (4B reasoning 3.3/10) | `af48bc2` |
| Manifest | YouTube Data/Analytics, Wikimedia Pageviews, GDELT, extra Wikipedia hosts; corrected YouTube quota text | streams A/C/E | `7f09f31`, `74fb0d2` |
| Sign-off (2026-11-13) | PR #5 base (`claude/hopeful-allen-luubcy`, contains PR #4) merged into the A3 branch; 9 conflicts, all sides kept | GitHub `mergeable: false` | `5034c8d` |
| Sign-off | batch_llm checkpoint-resume integration test; growth evidence carries data origin; handoff under 1,500 words after the three-way merge | run r4 | `06def55`, `f34d9b8`, `0d9d1de` |
| Sign-off | base commit `a889cd8` merged (`4486de4`); run r4b: A1 `f9d8f11`, A2 `d979df1`, A3 `fae6d38`: 867 passed, 8 skipped, 0 failed (local Linux); CI not starting | integration | `integration/contracts_v1/RESULT.md` |

## Account 1 (2026-11-12): cloud platform — branch work/account-1-cloud-platform, PR #4
Lead verified real Kaggle GPU access first, then built the shared foundation (contracts v1, vault, migration 2, events,
owner auth). Four sub-agents in separate worktrees: A security/architecture, B compute (Kaggle), C storage/Drive/ingest,
D Studio/scheduler. Three hit the 5-hour usage limit and were resumed after the reset; all delivered. Lead integration
found and fixed: CI backup-test regression, Studio CSRF/session mismatch, token-shaped literal in a test, a non-contract
quota response, unauthenticated legacy routes. Demos C, D (worker loss + control-plane SIGKILL), F, G done for real;
A done on the dev host; B and the Drive legs of E blocked on the owner's Google OAuth client. Fast suite 392 passed.
Details: docs/ACCOUNT1_EXECUTION_REPORT.md, docs/ACCOUNT1_ACCEPTANCE_RESULTS.md.

**2026-11-13 — Account 1 final closure (lead, single session).** Refreshed GitHub state: PR #4 b2517b2 green, no reviews;
PR #5 24b3f71 green; PR #6 9929512 red (Account 2's undeclared CI deps). Added the `batch_llm` worker task (Kaggle-only,
bounded, checkpointed parts; live on T4 x2 with Qwen2.5-0.5B-Instruct, 8/8), declared Wikidata in the Cost Guard (GDELT
stays with Account 3), made the cookie/CSRF test deterministic, and added a control-plane-restart regression test. Built
the three-account integration at exact heads with `apply.sh`: 663 passed with the packages present; with CI deps only,
27 Account 2 tests fail on scipy/cv2/onnxruntime (fix verified). Clean-clone release check at 9ea4a73: 412 passed,
4 skipped. Verdict and details: docs/ACCOUNT1_FINAL_RELEASE_AUDIT.md.

**2026-11-13 — Account 1 closure round (docs/state only, no code change).** The owner closed Kaggle (accepted as-is),
and the docs and task registry now record it. CI on `e9ad5e9` is green: Linux and Windows 411 passed / 5 skipped. The
dev-container count (412 / 4) is reconciled: same 416 tests, with font and rclone skips mapped from the CI progress
output. Integration at the latest exact heads A1 `e9ad5e9` + A2 `a52a24a` + A3 `fd71471` + `apply.sh`: 748 passed,
0 failed, 12 skipped (Linux, CI deps). PR #6 is green (Windows failures fixed by Account 2). The Account 3
`account1_manifest` mismatch is still open; Account 3 was notified on PR #5. Audit: docs/ACCOUNT1_FINAL_RELEASE_AUDIT.md.

**2026-11-13 — Account 1 production closure (lead, single session).** PR #4 was at `919f158` (green), PR #5 at `fd71471`
(Account 3's `batch_llm` manifest still incompatible), Account 2 at `d979df1`. Reproduced Account 2's cookie/CSRF report:
httpx 0.28 sends the server-set and the manually set cookie together and the old test's verdict depended on the
parser's order (test ambiguity, not a bypass); fixed in the server too (any presented value must carry a valid
signature). Added the production profile (`NEXUS_ENV=production`, 12 startup gates, `production-check`), readiness
probe, single-instance lock, lifespan audit/unclean-restart detection, trusted-proxy rate-limit identity, request/upload
limits, vault ciphertext in backups (never the key) with restore validation, offline vault-key export/verify, backup/
auth/disk alerts, log rotation, refusal of credentials in job manifests. 16 new tests (`test_production.py`). Runbooks:
`ACCOUNT1_PRODUCTION_DEPLOYMENT.md`, `ACCOUNT1_DISASTER_RECOVERY.md`, `ACCOUNT1_OAUTH_PRODUCTION_CHECKLIST.md` (Google
facts re-checked). Fresh local integration at A1 `34a5ba1` + A2 `d979df1` + A3 `fd71471` + `apply.sh`; results in the
audit §7. Nothing merged, deployed or changed on Account 2/3 branches; no Kaggle change.
