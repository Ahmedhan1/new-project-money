# Phase 3 — production power (v0.3.0)

Owner request (2026-11-12): much stronger editing/montage, sound that is right, scenes that hang together, far better
production efficiency, tools to rework existing videos/films, and learning from content that performs. This file is the
plan and the shared contract for the agents implementing it. Task ids are in `nexus/registry/tasks.yaml`.

## Boundary that does not move
NEXUS reworks footage the user **owns, has licensed, or that is public domain**, and makes commentary/summary videos whose
substance is new (own narration, analysis, graphics). It does **not** contain anything whose purpose is to get someone else's
video past a platform's copyright or reused-content detection (no mirroring/pitch/speed tricks, no fingerprint evasion, no
platform downloaders). Those techniques get channels struck and demonetised; the durable route to "republish without problems"
is rights + real transformation, which `nexus/repurpose.py` measures and reports.
Nobody can promise millions of views. What can be engineered: strong hooks, pacing, captions, loudness, platform fit,
many testable variants, and learning from measured results.

## Work packages (disjoint files; integration by the lead)
| Id | Package | Files owned | 
|---|---|---|
| FX-1 | Effects, transitions, animated captions, SFX | `nexus/fx/*`, `nexus/timeline/render.py`, `tests/fast/test_fx.py` |
| PERF-1 | Render efficiency: parallel segments, captions burned per segment so the final pass copies video, draft mode, hardware-encoder detection, benchmark | same owner as FX-1; `docs/results/perf_render.json` |
| GRW-1 | Shorts extractor, retention rubric + auto retention pass, hook/title variants, reference-video style profile | `nexus/growth.py`, `nexus/shorts.py`, `tests/fast/test_growth.py` |
| RPR-1 | Repurpose studio: rights gate, commentary/recap layouts, transformation report | `nexus/repurpose.py`, `tests/fast/test_repurpose.py` |
| AUD-1 | Voice mastering chain per narration, SFX/ambience helpers, scene-to-scene continuity QA | `nexus/audio/master.py`, `nexus/qa_continuity.py`, `nexus/speech/tts.py`, `tests/fast/test_audio_continuity.py` |
| INT-3 | API endpoints, job handlers, UI pages, presets, docs, handoff | lead: `api.py`, `workflows.py`, `ui/index.html`, `__main__.py`, registry, docs |

## Timeline contract (additive; unknown keys must keep being ignored)
```
clip["effects"] = [ {"type": "punch_in", "start": 1.0, "end": 2.5, "scale": 1.15},
                    {"type": "zoom", "from": 1.0, "to": 1.2}, {"type": "shake", "start":..,"end":..,"amount": 6},
                    {"type": "vignette"}, {"type": "grain", "amount": 8}, {"type": "letterbox", "ratio": 2.39},
                    {"type": "blur", "start":..,"end":..}, {"type": "flash", "at": 0.0}, {"type": "glitch", "start":..,"end":..},
                    {"type": "speed_ramp", "from": 1.0, "to": 0.5, "start":..,"end":..} ]      # times are clip-local seconds
clip["transition"] = "crossfade"; clip["xfade"] = any name in fx.transitions.NAMES; clip["transition_dur"]
clip["pip"] = {"src": path, "in": 0, "x": 0.68, "y": 0.06, "w": 0.28, "shape": "rect|circle"}   # picture-in-picture
tl["captions"] = {"style": "classic|boxed|pop|karaoke", "position": "bottom|middle|top", "words_per_cue": 3,
                  "highlight": "#FFD400", "uppercase": false, "font_scale": 1.0}
tl["subtitles"][i]["words"] = [{"start","end","word"}]      # optional word timings (programme time) for pop/karaoke
tl["overlays"] = [ {"type": "progress_bar", "color": "#3EC5FF", "height": 6}, {"type": "watermark", "text": "..."} ]
tl["audio"][i]["role"] = "narration|sfx|ambience"; fx.sfx.make(name, **kw) -> wav path  (whoosh, impact, riser, pop, click, swell)
render.render(tl, out, workdir, on_progress=None, draft=False, workers=None) -> same stats dict + {"draft", "workers"}
```
`segment_key` must include every key that changes pixels or sound so the per-scene cache stays correct.

## Rules for implementing agents
- Read `AGENTS.md`. Zero spend, no new network hosts, no model downloads, no heavy tests A–I, no commits or pushes (the lead commits).
- Use `.venv/bin/python`. Before reporting: `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest tests/fast -q` must pass.
- Fast tests build their own media with FFmpeg `lavfi` sources; they must not need models or network.
- Touch only the files you own. If you need a change elsewhere, describe it in your report instead of making it.
- Report honestly: what was measured (numbers), what is untested, known limits. No claim without a test or a measurement.

## Phase 3b (owner request, 2026-11-12, second message)
| Id | Package | Files owned |
|---|---|---|
| LANG-1 | 12+ languages (priority: English, German, French, Egyptian Arabic, Gulf Arabic, Chinese, Russian, Hindi), per-language voices with verified licences, fonts/shaping, per-language QA | `nexus/registry/languages.yaml`, `nexus/registry/models.yaml`, `nexus/models.py`, `nexus/localize.py`, `nexus/speech/mt.py`, `nexus/speech/asr.py`, `nexus/config.py` (fonts only), `nexus/timeline/text.py`, `nexus/timeline/subs.py`, `tests/fast/test_languages.py`, `tests/test_c_language.py` |
| PLAT-1 | Platform knowledge base (specs + publicly documented ranking signals with sources and dates) and per-platform tuning of a timeline | `nexus/registry/platforms.yaml`, `nexus/platforms.py`, `nexus/publish.py`, `tests/fast/test_platforms.py` |
| DIR-1 | Director: long footage -> condensed cinematic master + platform cuts + shorts, filler/pause removal, scene compression to a target length | `nexus/director.py`, `nexus/edit.py`, `tests/fast/test_director.py` |

Interfaces other packages provide (import lazily, tolerate ImportError):
`shorts.find_moments(analysis, min_s=18, max_s=58, top=5)`, `shorts.build_short(src_path, analysis, moment, size=(1080,1920), style=None)`,
`shorts.make_shorts(project, media, analysis, n=3)`, `growth.retention_report(tl, analysis_by_src=None, platform="shorts")`,
`growth.apply_retention_pass(tl, report)`, `presets.get(name)`, `presets.apply(tl, style)`, `config.font_for(lang)` (font file),
`config.font_name_for(lang)` (fontconfig family name for ASS; added by LANG-1).
Platform ranking systems are not public. Only signals the platforms themselves have documented may be encoded, each with its
source and the date it was read; everything else is labelled an editorial rule of thumb.
