# Preserved work from session 3 (branch `claude/intelligent-faraday-y28cx1`, never merged)

`session3-generation-work.patch` is the uncommitted work of the session that ran on 2026-11-12 05:30–06:40 UTC on
top of `23caae9` (before PR #1 was merged and before the 98 commits of `claude/hopeful-allen-luubcy`). It was not
committed when that session's worker restarted, and PR #1 was merged meanwhile, so it is kept here as a patch
instead of being forced into code that has since moved on. Nothing in it is applied.

| Part | Owner now | Content | Evidence when it was written |
|---|---|---|---|
| `nexus/generate/{image,video,shots,select}.py`, `registry/genmodels.yaml`, `scripts/bench_generation.py`, `docs/GENERATION.md` | **Account 2** (generation) | CPU image generation (DreamShaper-8 + LCM-LoRA, 32.7 s per 768x432 image), AnimateDiff-Lightning text-to-video (2 s clip in 115 s, 11 GB RAM with slicing), depth-parallax camera moves (Depth Anything V2 Small, 1.5 s per 4 s 720p shot), licence-gated model/compute selector over 19 models (Wan, LTX, HunyuanVideo, CogVideoX ...), multi-shot scene director, series character sheets | Test D and Test F passed with generated shots on the old base; frames inspected |
| `nexus/storage/__init__.py` (`GitBackend`), `registry/storage.yaml`, backup flush | **Account 1** (storage) | Encrypted off-machine backup to a branch of a private Git repo, restore on a fresh machine | `test_offsite_git_backup_restores_on_a_fresh_machine` passed |
| `nexus/bench.py`, `nexus regress` | shared / Account 3 measurement | Fixed-case quality regression (visual, motion, story, voice, audio, subtitles, technical) with baseline + history | `tests/fast/test_bench.py` passed |
| `.github/workflows/windows-launchers.yml`, `.bat` `:hold` change | Account 1 | Runs the real launchers on windows-latest | not run |

To reuse a part: `git apply --3way --include='nexus/bench.py' docs/handoff/session3-generation-work.patch` (paths are
relative to the repository root), then reconcile with the current code and re-run its tests.
