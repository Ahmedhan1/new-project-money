# Performance — measured, not estimated

Machine for every number below: cloud dev VM, 4 CPU threads, 16 GB RAM, **no GPU**. The user's Windows PC will differ —
re-measure there (`python -m nexus profile` after producing a video; task `CAP-1`).

## Create-from-idea, one master (Test D, 2026-11-11: 4 scenes, 47.2 s of 720p video)
| Stage | Seconds | Share | What dominates |
|---|---|---|---|
| research | 44.2 | 20 % | Wikipedia API round-trips incl. rate-limit back-off (network-bound, not CPU) |
| script (write + review) | 60.3 | 28 % | Local LLM: prompt evaluation of ~1,200 tokens of notes + ~250 generated tokens at **5.0 tok/s** |
| assemble (TTS, image search/download, music, storyboard) | 61.6 | 28 % | Stock search ladder + downloads; TTS itself is fast (**0.074× real time**) |
| render + QA | 50.0 | 23 % | Encode at **0.76× real time** + full decode/loudness/black/freeze checks |
| **Total** (incl. model load) | **217** | | **4.6× the video's duration** |

So this profile — narrated stills, one language — costs about **3.6 minutes per master**; five masters ≈ 18 minutes of
machine time. That is *not* the hard part of "five premium videos a day".

## What five-per-day really depends on (profiles are never mixed up — `python -m nexus capacity`)
| Profile | Status | Feasible at 5/day on 4 CPU threads? |
|---|---|---|
| Localized version of an existing master | built | Yes — but it is **not a new master** and is never counted as one |
| Remix of supplied footage | built | Yes |
| Template-assisted explainer (stills + motion graphics + narration) | built, measured above | Yes (minutes each) |
| Mixed production with 2D/3D animation inserts | built (Manim; Blender ≈ 3 s/frame on CPU) | Limited: a 10 s 3D insert ≈ 15 min of render |
| Fully AI-generated cinematic video | **not built** | No on CPU. Needs a GPU worker (task `GEN-2`, blocked on a free Kaggle account) |

The binding constraints for *premium* output are visual generation and human review time, not pipeline throughput.

## Optimisations in place
- Per-segment render cache (unchanged scenes are stream-copied; `segments_from_cache` in every render result).
- Stage checkpoints: a retry or crash never repeats research, script or narration.
- One heavy job at a time across processes (`jobs.heavy_lock`) — parallel LLM + encode on 4 threads is slower than serial.
- LLM `/no_think` (3–5× faster on CPU) and explicit model unload before rendering.
- Stock search stops querying a provider for the rest of a job once it is down (no 20 s time-outs per scene).

## Next, by expected gain
1. Cache research per topic and keep the LLM prompt prefix stable so llama.cpp reuses its KV cache across the write →
   rewrite rounds (script stage −30–50 %).
2. Run image search for all scenes concurrently (network-bound; assemble stage −40 %).
3. QA decode pass and loudness measurement in one FFmpeg invocation (render+QA −20 %).
4. Proxy (360p) preview renders for the editor.
