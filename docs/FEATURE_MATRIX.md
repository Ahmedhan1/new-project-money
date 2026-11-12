# NEXUS feature matrix (verified 2026-11-12, HEAD 52fbfd5, CI green on Linux + Windows)

States: **V** complete and verified (works in the integrated product on real media, evidence linked) · **I** implemented,
not fully verified (automated tests pass; not proven end to end on real media, or only on synthetic media) · **P** partially
implemented · **N** not implemented. Documentation and registry entries alone count as N.

| Area | Capability | State | Evidence / gap |
|---|---|---|---|
| Core | Durable jobs, stage checkpoints, crash recovery | V | Demo 5 (`docs/results/demos/demo5_recovery.json`), `test_jobs.py` |
| Core | Verified backups + restore drill | V | backup job VERIFIED this session; `test_storage_backup.py` |
| Core | Windows launchers on real Windows | P | Python layer in Windows CI; `.bat`/`.ps1` never run on real Windows (WIN-1) |
| Analysis | Transcription (Whisper small, CPU) | V | Demo 1 analysis (273 segments, 630 s for 19.4 min) |
| Analysis | Cross-segment sentences, topics, fillers, retakes, hooks | V | Demo 1 + `test_director.py` |
| Analysis | Speaker diarisation | N | heuristic interviewer detection only (DIA-1) |
| Analysis | Face/subject detection + tracking | N | no vision model; reframing uses fixed focus or blur-fill |
| Director | Plan with reasons, profiles, EDL, editable timeline | V | Demo 1, UI driven in Chromium |
| Director | B-roll / visual storytelling | P | plans cutaways only from the source's own low-speech shots; no asset library (BRL-1) |
| Renderer | Cuts, trim, split, ripple, move, insert, reframe | I | `ops.py`; ripple keeps audio/subs aligned; no track model |
| Renderer | Effects (punch-in, zoom, shake, vignette, grain, letterbox, blur, flash, glitch, speed ramp) | I | `test_fx.py` (subset asserted on pixels) |
| Renderer | xfade transitions, caption styles, animated titles, PiP, progress bar/watermark, colour correction, stabilise, rotate | I | `test_fx.py` on rendered pixels |
| Renderer | Chroma key, masks, split screen, freeze, credits, keyframes, J/L-cut audio, tracks | N | builders exist for some (`fx/post.py`), not wired (FX-2) |
| Renderer | Parallel segments, draft mode | I | measured 154 s -> 133 s on 4 cores |
| Audio | Loudness normalisation, ducking (role-aware), level matching at cuts | V | Demo 1 quality: level jumps reduced; −14 LUFS |
| Audio | Denoise / de-reverb / dialogue isolation | P | afftdn only; noise floor fails on every real render (AUD-2) |
| Shorts | Moment selection, context check, hook variants, captions, batch | V | Demo 2 (6 rendered shorts, scored) |
| Shorts | Smart reframing on faces | N | fixed centre/focus or blur-fill (no face tracking) |
| Platforms | Profiles with evidence kinds, tune/check/deliverables, deliver job | I | `test_platforms.py`, deliver API test; not run on real footage |
| Languages | Subtitles in de/fr/ar-EG/ar-Gulf/zh/ru/hi from real MT | V | Demo 3; all scripts render |
| Languages | Dubbing with a commercially cleared voice | I | pipeline + licence gate tested; no real dubbed demo this session |
| Languages | Native-speaker review workflow | N | flagged as required, no UI (LANG-2) |
| Quality | Weighted score, reasons, not-measured list, render-job stage | V | found and drove 6 real fixes |
| Quality | Calibration against human-reviewed examples | N | thresholds are engineering defaults |
| Media | Encoder detection, size-targeted export, verify, proxies | I | `test_media_engine.py`; not used by the deliver job yet |
| Generation | Image generation (SD 1.5 + LCM, CPU) | V | Demo 4, `docs/results/generation_bench.json` |
| Generation | Text/image-to-video | N | no GPU; never benchmarked (GEN-2) |
| Publishing | Upload / analytics | N | blocked on owner OAuth (PUB-1, ANA-1); packages only |
| UI | Director, decisions, EDL, draft, quality, shorts, destinations | V | Chromium run on the real project |
| UI | Multi-track timeline, captions editor, audio panel | N | |
