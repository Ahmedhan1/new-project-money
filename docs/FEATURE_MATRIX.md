# NEXUS feature matrix (re-verified 2026-11-12 13:30 UTC, branch `claude/hopeful-allen-luubcy`)

States: **V** complete and verified (works in the integrated product on real media, evidence linked) · **I** implemented,
not fully verified (automated tests pass; not proven end to end on real media, or only on synthetic media) · **P** partially
implemented · **N** not implemented. Documentation and registry entries alone count as N.

Real media used for verification: a 19.4-minute NASA interview (public domain, NASA media guidelines) — project
`prj_2b1a5a2f07`, outside Git. Machine: 4 vCPU, 15 GB RAM, no GPU.

| Area | Capability | State | Evidence / gap |
|---|---|---|---|
| Core | Durable jobs, stage checkpoints, crash recovery | V | `acceptance_recovery.json`: 5 production jobs hard-killed and resumed to completion (renders reused 41/44 and 44/44 segments); fast-restart and zombie-process defects fixed (d0349b8, ee6f4cc) |
| Core | Verified backups + restore drill | V | backup job VERIFIED; Windows e2e ran `NEXUS_BACKUP --drill` |
| Core | Windows launchers on Windows | V (CI VM) | `windows-e2e.yml` run 43202615448: SETUP → START → API e2e (non-ASCII NEXUS_HOME, Arabic/Chinese text, render) → STATUS → BACKUP drill → STOP, all green on clean `windows-latest`. Not yet run on the owner's own PC |
| Analysis | Transcription (Whisper small, CPU) | V | Demo 1 (273 segments, 630 s for 19.4 min) |
| Analysis | Cross-segment sentences, topics, fillers, retakes, hooks | V | Demo 1 + `test_director.py` |
| Analysis | Speaker diarisation + interviewer/guest roles + manual correction | V | WeSpeaker ONNX (CC BY 4.0) + sentence-level voice voting (merged question+answer segments split, ba72c82); corrections and names in the Review page (REV-1) |
| Analysis | Face detection + subject tracking | V | YuNet (MIT): 99.3 % frame coverage on the interview (`faces_demo.json`); feeds shorts reframing |
| Director | Plan with reasons, profiles, EDL, editable timeline | V | Demo 1, UI driven in Chromium |
| Director | B-roll / visual storytelling from licensed libraries | V (limited) | NASA + Openverse with licence, credit, provenance; footage in-point from motion, 1280x720 window (c9dfcf7); reviewer can reject a cutaway. Matching is lexical, not visual (K-18, BRL-2) |
| Renderer | Cuts, trim, split, ripple, move, insert, reframe | I | `ops.py`; ripple keeps audio/subs aligned; no multi-track model |
| Renderer | Effects (punch-in, zoom, shake, vignette, grain, letterbox, blur, flash, glitch, speed ramp) | I | `test_fx.py` asserts on rendered pixels |
| Renderer | xfade transitions, caption styles, animated titles, PiP (incl. full-frame cutaway), colour, stabilise, rotate | V | used in the 4-min acceptance edit; pixel tests |
| Renderer | Chroma key, masks, split screen, freeze frame, credits roll, position/scale/volume keyframes, J/L-cut audio | I | wired into `render.py`, 20 pixel/audio tests in `test_fx.py`; credits used on real render, others synthetic only |
| Renderer | .cube LUT management (validate, library, built-ins, intensity) | I | `nexus/fx/lut.py`, pixel test; not yet used on a real edit |
| Renderer | Crop that follows a moving subject (face track -> keyframes) | I | `faces.follow_keyframes`, moving-subject pixel test; the NASA subject barely moves, so not exercised on real media |
| Renderer | Opacity keyframes, audio-reactive overlays, per-sample audio speed ramps, multi-track timeline, tracked masks | N | FX-3 |
| Renderer | Parallel segments, draft mode, segment cache | V | cache reuse proven in recovery demo |
| Audio | Loudness normalisation, ducking, level matching at cuts and between speakers | V | RMS-targeted leveller: interviewer/guest gap 7.5 dB -> 0 dB on the real span (7880635); level-jump flags 30 -> 6 (remaining = natural prosody) |
| Audio | Speech denoise (RNNoise) + gated leveller | V (minor gap) | pauses −39.3 → −42.1 dBFS on the 4-min edit; bar is −48 (measured over 0.6 s of short gaps) |
| Audio | De-reverb, stem/dialogue isolation from music | N | no licence-clean CPU model integrated |
| Shorts | Moment selection, context check, hook variants, captions, batch | V | `acceptance_shorts.json`: 8 distinct 22-43 s shorts, quality 89.7-98.1 (1 pass, 7 needs_work: source sharpness, caption-speed bursts), all with an out |
| Shorts | Face-tracked reframing; never open on the interviewer; titles | V / P | openings inspected on all 8; titles are verbatim fragments (K-23), editable in Review |
| Platforms | Profiles with evidence kinds, tune/check/deliverables, deliver job with export verify + size cap | V | `acceptance_platforms.json`: 16:9/4:5/9:16 for 4 platforms, face-tracked crops, safe zones inspected on frames, caption.txt + hashtags from speech |
| Languages | Subtitles de/fr/ar-EG/ar-Gulf/zh/ru/hi from real MT, per-language QA, subtitle package | V | `acceptance_subtitles.json`: ready ar-EG/de/fr/hi; ar-Gulf/ru/zh flagged NEEDS_FIXES by QA |
| Languages | Egyptian vs Gulf Arabic as separate profiles | P | separate packs, lexicon checks; MT model has no dialect control, so Gulf output is mostly MSA (flagged) |
| Languages | Dubbing with a commercially cleared voice | V (needs review) | `acceptance_dub.json`: German AI voice-over, Piper `de_DE-thorsten` (CC0); no overlaps, speaker never below 0.85x; QA flags 8 MT lines + 3 long lines for a native reviewer |
| Languages | Native-speaker review workflow | I | Review page: approve/correct each line (machine text kept); fully approved branch -> `commercially_ready` in the package. Driven in Chromium on test data; no native reviewer has used it yet |
| Quality | Weighted score, reasons, not-measured list, render-job stage | V | drove the fixes listed in `docs/AGENT_WORK_LOG.md` |
| Quality | Calibration against human-reviewed examples | N | thresholds are engineering defaults |
| Media | Encoder detection, size-targeted export, verify, proxies | V | export verify + re-encode run inside the deliver job |
| Generation | Image generation (SD 1.5 + LCM, CPU) | V | `generation_bench.json` |
| Generation | Animated stills (Ken Burns) / motion graphics | V | used for B-roll stills; provenance kind `animated_still` |
| Generation | Text/image-to-video | N on this machine | AnimateDiff on CPU = noise (`t2v_cpu_bench.json`); `scripts/gpu_video_bench.py` (Wan 2.1 1.3B / 2.2 5B, Apache-2.0) ready for the owner's GPU; never claimed |
| Publishing | Upload / analytics | N | blocked on owner OAuth (PUB-1, ANA-1); packages only |
| UI | Director, decisions, EDL, draft, quality, shorts, destinations | V | Chromium run on the real project |
| UI | Review page (speaker names/roles/merge, translated lines, cutaways) | I | Chromium run on a throwaway project (`review_ui`), 4 API tests |
| UI | Multi-track timeline, captions editor, audio panel | N | |
