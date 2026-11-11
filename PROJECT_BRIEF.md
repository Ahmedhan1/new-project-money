# NEXUS VIDEO AI — Project Brief (permanent)

**What it is.** A local-first, zero-spend AI content production system: edit, remix and create videos (research → script →
storyboard → visuals → narration → music → edit → QA), animate (2D/3D/motion graphics), dub and subtitle in several
languages, plan series and films, package for platforms, and run as a small content factory.

**Non-negotiables**
1. **Zero monetary spending.** Only free/open-source software, local compute and legitimately free services, used within
   their terms and quotas. Anything that could bill is refused in code before it runs.
2. **GitHub is the source of truth** for code, docs, tests, schemas, manifests and project memory — not for media, model
   weights, databases or backups (those live in interchangeable storage; see `docs/STORAGE.md`).
3. **Recoverable.** No single chat session, machine, worker, drive or provider may be a point of failure for the code,
   the development state or the user's projects.
4. **Honest quality.** Output quality is judged on real renders with documented QA, never on the existence of a module.
5. **Simple to run.** One file starts it on Windows (`NEXUS_START.bat`); one document resumes development (`NEXUS_HANDOFF.md`).

**Long-term target.** Five premium *master* videos per day plus language/platform adaptations — subject to measured
compute and verified quality. Language versions and platform cuts are never counted as extra masters.

**Primary user.** One creator on a Windows PC, not a developer. Wants a working application, not terminal commands.

**Scope map.** The 40 product domains and their real status are in `docs/GAP_ANALYSIS.md`; the work queue is
`nexus/registry/tasks.yaml`.
