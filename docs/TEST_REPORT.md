# Test Report (updated 2026-11-11, v0.2.0)

Two suites:
- **Fast** — `python -m pytest tests/fast -q`: 28 tests, ~10 s, no models/network/GPU. Runs in CI on Linux + Windows.
- **Heavy A–I** — `tests/test_*.py`: real models and renders, minutes each, local only. Evidence in `docs/results/`.

## Final acceptance tests (prompt §15)
| # | Test | Result | Evidence |
|---|---|---|---|
| A | GitHub recovery: clean clone → install → diagnostics → start | **PASS** (Linux, light install without models) | Cloned into a scratch dir, `pip install -r requirements-ci.txt`, `nexus resume` printed the next task from files alone, `nexus start` → `/api/health` `{"ok":true,"version":"0.2.0"}`, `nexus doctor` green for startup/storage/project memory |
| B | Interrupted agent session: checkpoint → new session → resume | **PASS** | `tests/fast/test_state.py::test_checkpoint_commits_state_and_survives_no_network` (brief built from files only); this repo's own commits are made by `nexus checkpoint`; the clone in A resumed with zero chat context |
| C | Interrupted production: kill worker → restart → recover, finished stages not repeated | **PASS** | `test_jobs.py::test_worker_killed_mid_job_resumes_without_repeating_stages` (real process, hard kill: stage counts a=1, b=2, c=1) and `::test_create_pipeline_runs_as_resumable_stages` (research/script/assemble each ran once across a crash in render) |
| D | Storage recovery: backup → checksums → restore elsewhere → reopen → references | **PASS** | `test_storage_backup.py::test_backup_verify_restore_and_reopen_project` (original home deleted, project reopened from the restored copy, media/timeline/audio references valid) + `::test_corruption_and_missing_objects_are_detected` |
| E | Provider failure: one destination down, others continue, nothing silently lost | **PASS** | `::test_one_destination_down_does_not_lose_data` (unplugged drive → loud warning, other copy verified, catches up when back; all down → reported as failure) |
| F | Startup: reboot Windows → double-click `NEXUS_START.bat` → browser → previous project | **PARTIAL** | Python launcher: `test_launcher.py::test_start_health_previous_project_stop` (start, health, earlier project served, orphan job recovered, stop). **Not done:** the `.bat` on real Windows after a reboot |
| G | Quality regression vs. saved baselines | **NOT BUILT** | Baseline metrics exist in `docs/results/*.json`; comparison command is task `TST-R` |
| H | Zero-spend protection | **PASS** | Heavy Test H (`H_zero_spend.json`, previous session) + `::test_zero_spend_rules_for_storage`, `::test_free_allowance_cap_is_never_exceeded`, `::test_remote_backup_requires_encryption` |

## Heavy tests A–I
| Test | Scope | Result | When |
|---|---|---|---|
| A | Edit: analyse, propose, render, QA | PASS (`A_edit.json`) | session 1 — not re-run |
| B | Remix: 3 versions from 3 sources | PASS (`B_remix.json`) | session 1 — not re-run |
| C | Localization en→ar/de, dub, review | PASS (`C_language.json`) | session 1 — not re-run |
| D | Create from idea | see below | session 2 |
| E | Animation: Manim, Blender, motion graphics | PASS (`E_animation.json`) | session 1 — not re-run |
| F | Film plan + 2 series episodes | see below | — |
| G/H/I | Factory, zero-spend, compute failover | PASS (`G_factory.json`, `H_zero_spend.json`, `I_failover.json`) | session 1 — not re-run |

Session-1 results were **not re-executed** this session (their result files are the evidence). Modules they cover that
were touched this session: `create.py`, `assets.py`, `llm.py` (covered by D), `api.py` job submission (covered by fast
tests), `config.py` fonts (A/C/E use them — re-run A, C, E once before the next release: task `TST-R`).

## Test D — create from idea (session 2): PASS after three real fixes
`docs/results/D_create.json`, `D_storyboard.png`, `D_frame0-2.png`. Topic "How vaccines train the immune system", 4 scenes.

| Run | Outcome | What it showed |
|---|---|---|
| 1 | FAIL (video 18 s; test needs > 25 s) — yet QA said `PASSED_AUTOMATED_CHECKS` | One sentence per scene (36 words total); **0 licensed images** (every scene a gradient card): descriptive queries like "doctor giving BCG vaccine to child" return nothing |
| 2–3 | FAIL (0 scenes) | After a prompt change the 4B model answered `{}`; the pipeline rendered a 2.4 s title-only video |
| 4 | **PASS**, 217 s | 47.2 s video, 104 words, 2 sentences per scene on the first attempt, **3 of 4 scenes with licensed Commons images** + credits card, all 5 QA stages pass |

Fixes: grammar-constrained script schema (no empty/short output possible), scene-depth gate + reviewer feedback to the
writer, stock query ladder, `thumb.wikimedia.org` host. Guarded by `tests/fast/test_quality_guards.py`.

**Honest quality reading of run 4** (frames inspected): it is a competent narrated slideshow. Image relevance is
approximate (a histology slide of intestinal cells stands in for "immune cells"); scene 2 still fell back to a title
card; the script leans on the "Trained immunity" article rather than how vaccines work in general (K-4). Not premium.

## Test F — film plan + series (session 2): PASS on first run, 368 s
`docs/results/F_series.json`, `F_ep2_frame.png`. Film plan "The Keeper's Eye" (6 scenes, shot list). Series "Sunken
Currents": bible with 3 characters, 3-episode season; episodes 1 and 2 written, continuity-reviewed and rendered
(49.9 s and 41.8 s, both `PASSED_AUTOMATED_CHECKS`); all 3 characters recur; episode 2 opens with a recap and picks up
the cliffhanger; same look/sound identity; continuation metadata present.

**Honest quality reading:** the continuity checker correctly flagged that episode 2 is set in a location ("Lunar Reef")
that is not in the bible (reported as a minor issue, not fixed). Episode visuals are title cards with narration — no
characters are ever shown, so "character consistency" is textual only. Episode 2's scene lines are single short clauses
(the series writer does not yet use the schema/depth gate that fixed Test D → task `QLT-1`).

## Not covered by any test yet
Real Windows `.bat` run · any real cloud storage remote · analyze/remix/package job handlers with real models ·
quality regression baselines · UI beyond the Storage page smoke run.
