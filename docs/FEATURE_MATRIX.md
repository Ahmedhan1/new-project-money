# NEXUS feature matrix (re-verified 2026-11-12, branch `claude/hopeful-allen-luubcy`)

States: **V** complete and verified (works in the integrated product on real media, evidence linked) · **I** implemented,
not fully verified (automated tests pass; not proven end to end on real media, or only on synthetic media) · **P** partially
implemented · **N** not implemented. Documentation and registry entries alone count as N.

Real media used for verification: a 19.4-minute NASA interview (public domain, NASA media guidelines) — project
`prj_2b1a5a2f07`, outside Git. Machine: 4 vCPU, 15 GB RAM, no GPU.

| Area | Capability | State | Evidence / gap |
|---|---|---|---|
| Core | Durable jobs, stage checkpoints, crash recovery | V | `acceptance_recovery.json`; fast-restart defect found and fixed (d0349b8), `test_jobs.py` |
| Core | Verified backups + restore drill | V | backup job VERIFIED; Windows e2e ran `NEXUS_BACKUP --drill` |
| Core | Windows launchers on Windows | V (CI VM) | `windows-e2e.yml` run 43202615448: SETUP → START → API e2e (non-ASCII NEXUS_HOME, Arabic/Chinese text, render) → STATUS → BACKUP drill → STOP, all green on clean `windows-latest`. Not yet run on the owner's own PC |
| Analysis | Transcription (Whisper small, CPU) | V | Demo 1 (273 segments, 630 s for 19.4 min) |
| Analysis | Cross-segment sentences, topics, fillers, retakes, hooks | V | Demo 1 + `test_director.py` |
| Analysis | Speaker diarisation + interviewer/guest roles + manual correction | V | WeSpeaker ResNet34 ONNX (CC BY 4.0): 3 speakers, silhouette 0.72, guest 79 % talk share (`diarization_demo.json`); rename/merge/reassign in `diarize.py`. No UI for corrections yet |
| Analysis | Face detection + subject tracking | V | YuNet (MIT): 99.3 % frame coverage on the interview (`faces_demo.json`); feeds shorts reframing |
| Director | Plan with reasons, profiles, EDL, editable timeline | V | Demo 1, UI driven in Chromium |
| Director | B-roll / visual storytelling from licensed libraries | V (limited) | NASA Image & Video Library + Openverse with per-item licence, credit, provenance; 16 cutaways / 27 % share on the 4-min edit. Matching is lexical (title/keywords), not visual: relevance still needs a human glance |
| Renderer | Cuts, trim, split, ripple, move, insert, reframe | I | `ops.py`; ripple keeps audio/subs aligned; no multi-track model |
| Renderer | Effects (punch-in, zoom, shake, vignette, grain, letterbox, blur, flash, glitch, speed ramp) | I | `test_fx.py` asserts on rendered pixels |
| Renderer | xfade transitions, caption styles, animated titles, PiP (incl. full-frame cutaway), colour, stabilise, rotate | V | used in the 4-min acceptance edit; pixel tests |
| Renderer | Chroma key, masks, split screen, freeze frame, credits roll, position/scale/volume keyframes, J/L-cut audio | I | wired into `render.py`, 20 pixel/audio tests in `test_fx.py`; credits used on real render, others synthetic only |
| Renderer | Opacity keyframes, audio-reactive overlays, per-sample audio speed ramps, multi-track timeline | N | documented as not wired in `docs/TIMELINE.md` |
| Renderer | Parallel segments, draft mode, segment cache | V | cache reuse proven in recovery demo |
| Audio | Loudness normalisation, ducking (role-aware), level matching at cuts | V | −14 LUFS; level jumps reduced |
| Audio | Speech denoise (RNNoise) + gated leveller | V | noise floor −39.9 → −43.8 dBFS, SNR 25.3 → 29.1 dB (`audio_post.json`) |
| Audio | De-reverb, stem/dialogue isolation from music | N | no licence-clean CPU model integrated |
| Shorts | Moment selection, context check, hook variants, captions, batch | V | `acceptance_shorts.json` |
| Shorts | Face-tracked reframing; never open on the interviewer | V | YuNet focus spans; interviewer hook cap + guest-only titles (0f1a991) |
| Platforms | Profiles with evidence kinds, tune/check/deliverables, deliver job with export verify + size cap | V | `acceptance_platforms.json` |
| Languages | Subtitles de/fr/ar-EG/ar-Gulf/zh/ru/hi from real MT, per-language QA, subtitle package | V | `acceptance_subtitles.json`: ready ar-EG/de/fr/hi; ar-Gulf/ru/zh flagged NEEDS_FIXES by QA |
| Languages | Egyptian vs Gulf Arabic as separate profiles | P | separate packs, lexicon checks; MT model has no dialect control, so Gulf output is mostly MSA (flagged) |
| Languages | Dubbing with a commercially cleared voice | V | `acceptance_dub.json`: German AI voice-over, Piper `de_DE-thorsten` (licence verified); labelled AI voice-over, native review required |
| Languages | Native-speaker review workflow | P | `native_review_required` in package manifest; no review UI (LANG-2) |
| Quality | Weighted score, reasons, not-measured list, render-job stage | V | drove the fixes listed in `docs/AGENT_WORK_LOG.md` |
| Quality | Calibration against human-reviewed examples | N | thresholds are engineering defaults |
| Media | Encoder detection, size-targeted export, verify, proxies | V | export verify + re-encode run inside the deliver job |
| Generation | Image generation (SD 1.5 + LCM, CPU) | V | `generation_bench.json` |
| Generation | Animated stills (Ken Burns) / motion graphics | V | used for B-roll stills; provenance kind `animated_still` |
| Generation | Text/image-to-video | P (unusable on CPU) | AnimateDiff ran on CPU: 8 frames in minutes, output is noise (`t2v_cpu_bench.json`). Needs a GPU; never claimed |
| Publishing | Upload / analytics | N | blocked on owner OAuth (PUB-1, ANA-1); packages only |
| UI | Director, decisions, EDL, draft, quality, shorts, destinations | V | Chromium run on the real project |
| UI | Multi-track timeline, captions editor, audio panel, diarisation correction | N | |
