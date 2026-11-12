# Timeline schema (renderer contract)

A timeline is a JSON dict. It is **non-destructive**: sources are never modified, every automated decision stays editable
with `nexus/timeline/ops.py`, and unknown keys are ignored by the renderer (but included in segment cache keys, so a new
pixel-changing key can never be served a stale segment). Renderer: `nexus/timeline/render.py`, tests: `tests/fast/test_fx.py`.

`render.render(tl, out, workdir, on_progress=None, draft=False, workers=None, checkpoint=None) -> {"output","duration",
"elapsed","segments","segments_from_cache","sidecars","draft","workers","notes","pieces","pieces_from_cache",
"audio_from_cache","timings","pipeline","resumed","checkpoint"}`. Pipeline, cache keys and resume model:
`docs/ACCOUNT2_RENDERER_ARCHITECTURE.md`; examples of every key: `docs/ACCOUNT2_EDITING_GUIDE.md`.

## Timeline keys
| Key | Type / default | Meaning |
|---|---|---|
| `width`, `height`, `fps` | int, 1920/1080/30 | Output geometry. |
| `lang` | `"en"` | Caption font/shaping, title text. |
| `grade` | `"none"` | Creative look for every clip without its own: `none cinematic warm cool bw vivid documentary` + film looks in `fx.post.GRADES` (`teal_orange`, `bleach_bypass`, ...). |
| `audio_fx` | `"none"` | `none denoise voice` chain on clip audio. |
| `target_lufs` | -14.0 | Two-pass loudness normalisation target; true peak -1.5 dBTP, limiter after. |
| `clips` | list | Played in order (see clip keys). |
| `audio` | list | Extra audio items `{"src","start","volume"=1,"role"}`. `role`: `narration`/`dialogue`/`voice` (or none) key the music ducking; `sfx`, `ambience`, `music` are mixed in after ducking and never count as speech. |
| `music` | dict or null | `{"src","volume"=0.5,"duck"=true}`; looped, faded in 1 s / out 2 s, sidechain-ducked under speech. |
| `subtitles` | list | Cues `{"start","end","text","words"?:[{"start","end","word"}]}` in programme seconds. SRT/VTT sidecars are always written. |
| `burn_subtitles` | true | Burn cues into the picture. |
| `captions` | dict or absent | Caption style for burn-in: `{"style": classic|boxed|pop|karaoke, "position": bottom|middle|top, "words_per_cue": 2-4, "highlight": "#FFD400", "uppercase": false, "font_scale": 1.0}`. Absent = the legacy look. |
| `subtitle_style` | dict | Legacy style overrides (used when `captions` is absent). |
| `tracks` | list | Layers above the main storyline: video (z-ordered boxes with opacity and an effect stack), text, audio. See "Tracks" below. |
| `slowmo` | `"none"` | Slow-motion profile for clips below 1x without their own `interpolate`: `none`, `blend`, `optical_flow` (minterpolate mci, slow). |
| `render_pipeline` | `"pieces"` | `pieces` (cached pieces, stream-copy join) or `legacy` (xfade concat + full re-encode). |
| `overlays` | list | Programme-wide: `{"type":"progress_bar","color","height","position"}`, `{"type":"watermark","text","opacity","position"}`. |
| `markers`, `chapters`, `director`, `repurpose`, ... | any | Metadata, not rendered. |

## Clip keys
Types: `video {src,in,out}`, `image {src,duration}`, `color {duration,color}`. All times in seconds.

| Key | Applies to | Meaning |
|---|---|---|
| `speed` | video | Constant speed; audio follows with pitch-preserving `atempo`. |
| `fit`, `focus` | video/image | `cover` (crop at `focus` = [x,y] 0..1), `contain` (pad), `blur` (blurred fill behind). |
| `mute`, `volume`, `audio_fx` | video | Clip audio. |
| `fade_in`, `fade_out` | all | Picture + sound fades. |
| `transition`, `xfade`, `transition_dur` | all but first | `"crossfade"` with any name in `fx.transitions.NAMES` (`fade`, `wipeleft`, `dissolve`, ...). Overlaps the previous clip by `transition_dur`. |
| `grade` | all | Per-clip creative look. |
| `correct` | all | Technical colour correction before the grade: `exposure` (stops), `contrast`, `brightness`, `saturation`, `temperature` (K), `vibrance`, `lift`/`gamma`/`gain` (scalar or [r,g,b]), `denoise` 0..1, `sharpen` 0..1. |
| `effects` | video (most also image/color) | List of `{"type": ...}`: `punch_in {start,end,scale}`, `zoom {from,to}`, `shake {start,end,amount}`, `vignette`, `grain {amount}`, `letterbox {ratio}`, `blur {start,end,amount}`, `flash {at,duration}`, `glitch {start,end}`, `speed_ramp {from,to,start,end}`. Effect times are clip-local output seconds (speed_ramp: pre-ramp clip seconds). Unknown types are ignored. |
| `stabilize` | video | `true` or `{"smoothing","shakiness","zoom","method": auto|vidstab|deshake}`. vidstab is two-pass and its motion analysis is cached. |
| `rotate`, `hflip`, `vflip` | video | 90/180/270 lossless; small angles level the horizon (crop-zoomed). Mirroring is for the user's own footage; the repurpose rights gate refuses it on third-party material. |
| `interpolate` | video | `blend` or `mci` frame synthesis for slow motion (off by default; `mci` is slow on CPU). |
| `text` | all | Static overlays `[{"text","style": title|lower_third|..., "start","end","lang"}]`. |
| `titles` | all | Animated titles/lower thirds `[{"text","style","start","end","anim": fade|slide|rise|none,"from": left|right}]`. |
| `pip` | all | Picture-in-picture / cutaway `{"src","in","x","y","w","h"?,"shape": rect|circle,"start","end"}`; `w`=1, `x`=`y`=0 is a full-frame cutaway over the clip's own audio (how the director inserts B-roll). |
| `notes`, `label` | all | Metadata (excluded from the cache key). |

## Post-production keys (wired, tested on rendered output in tests/fast/test_fx.py)
| Key | Applies to | Meaning |
|---|---|---|
| `freeze` | video | `{"at", "duration"}` or a list: hold the frame at clip-local `at`; audio is silent under the hold; clip duration grows. |
| `split` | video | `{"srcs": [path or {"src","in"}], "layout": "h"|"v"|"grid", "gap", "color"}`: the clip is panel 1. |
| `chroma_key` | all | `{"color": "#00FF00", "similarity", "blend", "despill", "bg": image/video path or "#RRGGBB"}`. |
| `mask` | all | `{"shape": rect|ellipse, "x","y","w","h" (0..1), "feather", "effect": black|blur|dim|pixelate, "region": outside|inside, "amount"}` or a list. |
| `credits` | all | `{"title", "lines": [...]}` rolled from bottom to top over the clip. |
| `keyframes` | video (x/y/scale with fit cover), all (volume) | `{"x"|"y": [[t, 0..1, "linear"|"ease"], ...], "scale": [[t, >=1], ...], "volume": [[t, gain], ...]}` over clip-local time; x/y move the crop window (reframing), scale zooms. |
| `audio_lead` | video | J-cut: the clip's audio starts this many seconds before its picture (taken from the source before `in`), the previous clip's audio fades out under it. |
| `audio_tail` | video | L-cut: the clip's audio continues this many seconds under the next picture (taken from the source after `out`). |
| `pip.full` | all | Full-frame cutaway (B-roll): cover-cropped, short fades, no border; the clip's own audio continues. |
| `lut` | all (also timeline-wide) | 3D LUT after correction and grade: a built-in name (`identity warm_film teal_orange bleach_bypass rec709_lift`, generated by NEXUS), a library name (`nexus.fx.lut.install`, validated `.cube` in `NEXUS_HOME/assets/luts`), a path, or `{"name"|"path", "intensity": 0..1}`. Cache key uses the file's content. |
| `gain_spans` | video | `[{"in","out","db"}]` in source seconds: per-speaker level matching, applied before the clean-up chain (`audio.post.apply_speaker_gains`). |
| `keyframes` from a face track | video | `platforms.tune` sets x/y keyframes from `faces.follow_keyframes` when the tracked subject moves inside a clip during a reframe (dead zone 3 %). |

| `opacity`, `keyframes.opacity` | all | 0..1, the clip over black. |
| `keyframes.rotation`, `rotation_fill` | all | Degrees clockwise over clip time; `zoom` (default, no corners) or `black`. |
| `perspective` | all | `{"tl","tr","bl","br": [x, y]}` (0..1): the quad that is mapped onto the full frame (keystone correction). |
| `mask.keyframes` | all | `{"x"|"y"|"w"|"h": [[t, v], ...]}` moving / tracked mask (`fx.post.mask_from_track` builds them from a face track). |
| `split.layout` | video | also `main_left`, `main_right`, `main_top`, `thirds_h`, `thirds_v`. |

## Tracks (multi-track timeline, `nexus/timeline/tracks.py`)
`tl["tracks"]`: `{"id","kind": video|text|audio,"z","opacity","keyframes":{"opacity"},"hidden","mute","volume",
"effects","grade","correct","lut","chroma_key","mask","items": [...]}`. The main `clips` are layer 0 and set the
programme length; video/text layers draw in ascending `z`. Video items: `{"type","src","in","out"|"duration","start",
"x","y","w","h","opacity","keyframes":{"opacity","pos_x","pos_y", + clip keyframes},"use_audio", + any clip key}`;
text items: `{"text","subtitle","style","start","end","anim","from","opacity"}`; audio items: `{"src","start","in",
"out","volume","role","fade_in","fade_out"}`. Ops in `ops.py`: `add_track`, `remove_track`, `set_track`, `add_item`,
`remove_item`, `move_item`, `set_item`, `set_keyframes`. Interchange: `timeline/edl.py` (CMX3600 EDL, OTIO JSON).

Still not wired: the audio-reactive waveform overlay (`fx/post.audio_viz*`), true per-sample speed-ramp audio.

## Draft and parallel rendering
`draft=True` renders at half resolution with a fast preset into separate cache entries (for previews).
`workers` segments render at once (default `min(4, cpu/2)`); FFmpeg threads are divided between workers.
Measured numbers: `docs/results/perf_render.json`.
