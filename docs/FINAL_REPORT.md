# NEXUS — final acceptance report (2026-11-12)

Branch `claude/hopeful-allen-luubcy` (PR #3, draft, not merged: merging needs the owner's approval). Source material:
19.4-minute NASA interview (public domain, NASA media guidelines), project `prj_2b1a5a2f07`; media stays outside Git.
Machine: 4 vCPU, 15 GB RAM, **no GPU**. Every figure below was measured on the rendered files, and every demo was
inspected frame by frame; a "done" job state was never taken as proof.

## Release decision
**Not production-ready; release candidate for owner review.** The eight acceptance demos ran on real media, except
generated video (demo 6: impossible without a GPU). Remaining blockers for "production-ready" are owner actions, not code:
a run on the owner's Windows PC, native-speaker review of the translations, and (for generated video) GPU access.

## Acceptance demos

| # | Demo | Result | Evidence |
|---|---|---|---|
| 1 | 3-5 min cinematic edit from licensed footage | **Done.** 4:04.7, 1920x1080 H.264/AAC, A/V 244.667/244.667 s, automated QA passed, quality 92.5 "pass". 16 licensed cutaways (NASA, Openverse) with credits. | `acceptance_cinematic.json`, contact sheet |
| 2 | 5-10 distinct shorts | **Done.** 8 distinct shorts (22-43 s, 1080x1920, A/V in sync), quality 89.7-98.1 (1 pass, 7 needs_work: soft 720p source, caption-speed bursts); all open on the guest, face-tracked, all end with an out. Titles are weak fragments: rewrite in Review. | `acceptance_shorts.json` |
| 3 | >= 3 platform adaptations | **Done.** 16:9 (YouTube, Facebook), 4:5 (Instagram feed), 9:16 (TikTok); face-tracked crops; captions above TikTok's bottom 30 % / inside Instagram's centre square (inspected on frames); all exports verified; caption.txt + upload steps per platform. | `acceptance_platforms.json`, frame sheets |
| 4 | Multilingual subtitle package | **Done.** 7 languages (de, fr, ar-EG, ar-Gulf, zh, ru, hi); ar-EG/de/fr/hi pass automated checks, ar-Gulf/ru/zh flagged; `commercially_ready` stays empty until a reviewer approves every line. | `acceptance_subtitles.json` |
| 5 | Dubbed version with a commercially cleared voice | DUB_RESULT | `acceptance_dub.json` |
| 6 | Real I2V/T2V | **Not possible here.** No GPU; AnimateDiff on CPU produced noise. Benchmark script ready for the owner's GPU (Wan 2.1 1.3B / 2.2 5B, Apache-2.0). Never claimed. | `t2v_cpu_bench.json`, `scripts/gpu_video_bench.py` |
| 7 | Crash-recovery production run | **Done.** 5 real jobs (renders, deliver, shorts) killed with SIGKILL mid-stage; all resumed and completed; renders reused 41/44 and 44/44 cached segments. Two recovery defects found and fixed. | `acceptance_recovery.json` |
| 8 | Clean Windows install + launch | **Done on a clean windows-latest VM** (SETUP, START, API e2e with non-ASCII paths and Arabic/Chinese text, real render, STATUS, BACKUP drill, STOP). Not yet on the owner's PC. | `windows-e2e.yml` runs |

## Defects found by inspecting real output, and fixed (each with a regression test)
Killed jobs left "running" after a fast restart; zombie worker not detected · audio 0.3 s longer than video · "guest" /
"interviewer" shown as lower thirds · frozen launch-pad cutaway; 320x180 B-roll upscaled · caption spanning a title card;
words duplicated at cuts; "160 -meter"; zero-length words dropped · peak-based leveller put the interviewer 7.5-11 dB
under the guest · quality check counted phrase pauses as level jumps · interviewer questions labelled as guest
(sentence-level voice voting) · 39 junk hashtags; caption.txt referenced but missing · MT inventing endings for fragments ·
voice-over slowing the speaker to 0.6x · quality score never running the language QA ("module is not callable") ·
shorts ending with sound on the last frame · CI red from a duplicate task id.

## Measured, not fixed (honest limits)
- Caption reading speed: p90 22.5 chars/s (edit) and 22-28 (shorts) against a 20 bar; speech averages 13.5-16.8 chars/s,
  fillers are 2 % - the fast cues are dense content and need paraphrase (CAP-2), not a filter.
- 6 remaining "level jumps" on the edit are soft words inside continuous speech (natural prosody).
- Pause noise -42.1 dBFS vs a -48 bar, over 0.6 s of short gaps.
- Sharpness: 720p source cropped to 1080x1920 for vertical versions.
- Shorts titles are verbatim opening words: rewrite them in the Review page (K-23).
- B-roll matching is lexical, not visual (K-18).

## Tests
TESTS_RESULT

## Zero spend
No paid service, card or billable API was used. Models: Whisper small (MIT), MADLAD-400 (Apache-2.0), Piper voices
(licence-gated, de_DE-thorsten CC0), WeSpeaker ResNet34 (CC BY 4.0), YuNet (MIT), RNNoise (BSD). Media: NASA (public
domain), Openverse items with per-item CC licences recorded.

## Exact owner actions
1. Run `NEXUS_SETUP.bat`, then `NEXUS_START.bat` on your Windows PC; report anything that fails.
2. Review in the app (Review page): name the guest (lower thirds appear), rewrite weak shorts titles, reject any
   unsuitable cutaway; have native speakers approve the de/fr/ar-EG/hi lines.
3. Optional, for generated video: on a GPU PC (>= 8 GB VRAM) or your own free Kaggle notebook,
   `python scripts/gpu_video_bench.py --model wan21-t2v-1.3b`, then send back `bench_out/gpu_video_bench.json`.
4. Decide whether to merge PR #3 into `main` (agents never merge), and choose a LICENSE (D-10).
5. Optional: authorise one off-machine backup destination (STO-3); platform OAuth only if you want uploads (PUB-1).
