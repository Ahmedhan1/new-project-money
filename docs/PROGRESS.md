# Progress (newest first)

**2026-11-11 — session 2 (v0.2.0): recoverability + first quality fixes**
- Recovered state: all prior work was on `claude/optimistic-franklin-376ndo` (2 commits); `main` held only a README.
  No handoff/state files existed; `nexus doctor` existed but Test D and F had no recorded results; jobs were an
  in-memory queue; model fetch crashed on a fresh clone.
- Built: project memory + checkpoint engine, durable job ledger + worker process, federated storage adapter, verified
  encrypted backups + restore + drill, one-command launcher, Windows launchers, Storage & Backup UI page, CI, 28 fast tests.
- Ran Test D for real: first result "passed QA" with an 18 s, one-sentence-per-scene video and zero images → fixed the
  script gate/feedback loop, schema-constrained LLM output, stock query ladder.
- Test F passed on its first run (film plan + 2 series episodes).
- Moved every API job onto the durable ledger (in-memory queue removed); automatic daily backups.
- CI green on Linux; Windows job surfaced two real file-locking issues in tests (fixed) and proved job recovery,
  encrypted backup/restore and the launcher on Windows.
- Wrote the 40-domain gap analysis and a 28-task registry.

**2026-11-11 — session 1 (v0.1.0): foundation**
- Cost guard, manifests, FFmpeg/ASR/TTS/MT, timeline renderer, analysis, edit, QA, localization, remix, create,
  animation, series, compute federation, factory, API, UI, research catalog. Heavy tests A, B, C, E, G, H, I passed;
  D and F unfinished.
