# CLAUDE.md

**Read NEXUS_HANDOFF.md and NEXUS_STATE.json before making any changes. Inspect the current Git status and continue from the last verified checkpoint. Never restart the project.**

Then follow `AGENTS.md` (rules for every coding agent). Claude-specific notes:

- First commands of a session: `git status -sb`, `git log --oneline -5`, `python -m nexus resume`.
- The work queue is `nexus/registry/tasks.yaml` (`python -m nexus tasks`). Take the first ready task; do not invent a new plan.
- Before your context or session runs out: update task statuses, run `python -m nexus checkpoint -m "<what changed>"`.
  Put state in files, not in a long chat summary.
- Cloud sessions are ephemeral: models (~7 GB) and `.nexus/` are not in Git. Rebuild with `scripts/setup.sh` only if the
  task needs real renders; the fast suite (`pytest tests/fast`) needs no models.
- Ask the user only for: payments (never allowed), account authorisation, irreversible deletion, public deployment,
  access to private data, or a genuine product decision. Everything else: decide, do, record in `docs/DECISIONS.md`.
