# Known Issues

| # | Issue | Impact | Workaround / plan |
|---|---|---|---|
| K-1 | `.bat`/`.ps1` launchers never executed on real Windows | Setup may fail on: `llama-cpp-python` wheel (uses a prebuilt-CPU index, unverified), Blender/eSpeak not on PATH, `curl` model download | Task WIN-1; the Python layer under them is CI-tested on Windows |
| K-2 | Running jobs cannot be cancelled (only queued ones); remix and analyze are single-stage jobs | A long remix restarts from its beginning after a crash (render segments are still cached) | Split remix into plan/render stages; add cooperative cancel |
| K-3 | Qwen3-4B answers `{}` to some prompts in free JSON mode | Script stage failed outright | Schema-constrained output + retry (D-9); a larger model is not an option on 4 cores |
| K-4 | Research takes Wikipedia's top search hits as-is | Script can drift to a neighbouring topic (Test D: "Trained immunity") | Task QLT-1: rank pages by title/topic overlap |
| K-5 | Stock search depends on Wikimedia Commons availability; Openverse returned HTTP 504 during testing | Scenes fall back to gradient title cards | Query ladder reduces misses; GEN-1 adds generated images |
| K-6 | Default backup destination is on the same disk as the data | A disk failure loses both | Add a second destination (`docs/STORAGE.md`); UI and `nexus status` warn |
| K-7 | Projects store absolute paths | Moving `NEXUS_HOME` by hand breaks references | Use `nexus restore` (rewrites paths) — or task: store project-relative paths |
| K-8 | `llama_cpp` prints `TypeError: 'NoneType' object is not callable` at interpreter exit | Cosmetic (destructor order in the library) | Ignore |
| K-9 | Compute federation's remote GPU is simulated | No real off-machine compute | Task GEN-2 (needs the user's free Kaggle account) |
| K-10 | Models and `.nexus` vanish with ephemeral cloud dev containers | Every new cloud session re-downloads ~7 GB to run heavy tests | Fast suite needs no models; run heavy tests only when the change requires it |
| K-11 | GitHub LFS/Actions quotas for this account were not read from the billing page | CI could exhaust free minutes if made heavy (Windows minutes count double on private repos) | CI is ~2 short jobs per push; user to check Settings → Billing |
| K-12 | Importing `nexus.api` starts an embedded job worker thread (and schedules backups) | A short script that imports the API can claim a job and die mid-stage; the ledger marks it lost and it can be retried (seen in session 4) | Scripts set `NEXUS_EMBEDDED_WORKER=0`; `nexus start` runs the worker as its own process |
| K-13 | Vertical crops of 720p landscape sources are soft | Shorts from a 1280x720 source use ~405x720 real pixels scaled to 1080x1920; quality score flags `visual.sharpness` | Use 1080p/4K sources for shorts; an upscaler (Real-ESRGAN) is in the generation registry but not wired |
| K-14 | MADLAD-400 occasionally truncates, loops or adds a sentence | Found in German (session 4); post-processing fixes the three observed patterns, language QA flags the rest | Native review before publishing; back-translation needs the local LLM |
| K-15 | Gulf Arabic = MSA machine translation + rule-based function-word substitutions | Reads largely as MSA | Native review required (pack flag); a Gulf MT model does not exist in the stack |
| K-16 | Parallel segment rendering gains little on 4 CPU cores (154 s -> 133 s on a 25-segment master) | x264 already uses all cores | Benefit expected with hardware encoders or more cores; measure on the owner's machine |
