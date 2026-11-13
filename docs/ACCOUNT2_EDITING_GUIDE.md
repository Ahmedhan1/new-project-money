# Account 2 editing guide

<!-- Sections are owned by different agents; the lead merges them. -->

## Renderer: every timeline key with an example (agent A)

A timeline is one JSON dict (`docs/TIMELINE.md` is the reference table). Edit it with `nexus/timeline/ops.py`
(every op returns a new dict; save each version with `Project.save_timeline` for undo/redo). Render with
`nexus.timeline.render.render(tl, out, workdir, draft=False, workers=None, checkpoint=None)`.
Times are seconds. "Clip time" = seconds from the start of that clip's output; "programme time" = seconds of the
finished video. Keyframes are always `[[t, value], [t, value, "ease"], ...]` (linear unless `"ease"`), held before the
first and after the last key. Keyframable properties: `fx.effects.KEYFRAME_PROPS`.

### Timeline-wide keys
```json
{"width": 1920, "height": 1080, "fps": 30, "lang": "en",
 "grade": "teal_orange", "lut": "warm_film", "audio_fx": "voice", "target_lufs": -14.0,
 "clips": [...], "tracks": [...], "audio": [...], "music": {"src": "bed.wav", "volume": 0.4, "duck": true},
 "subtitles": [{"start": 0.4, "end": 2.4, "text": "Hello"}], "burn_subtitles": true,
 "captions": {"style": "boxed", "position": "bottom", "words_per_cue": 3},
 "overlays": [{"type": "progress_bar"}, {"type": "watermark", "text": "@me"}],
 "slowmo": "optical_flow", "render_pipeline": "pieces"}
```
- `slowmo` (new): `none` (default) | `blend` | `optical_flow` - frame synthesis for every clip slowed below 1x that
  has no `interpolate` of its own. `optical_flow` = FFmpeg minterpolate (mci): smooth, much slower (see the benchmark).
- `render_pipeline` (new): `pieces` (default, cached pieces + stream-copy join) or `legacy` (kept for comparison).

### Main storyline clips (`clips`)
```json
{"id": "c1", "type": "video", "src": "/media/interview.mp4", "in": 12.0, "out": 18.5,
 "speed": 1.0, "fit": "cover", "focus": [0.5, 0.4], "volume": 1.0, "mute": false,
 "transition": "crossfade", "xfade": "wipeleft", "transition_dur": 0.5,
 "grade": "warm", "correct": {"exposure": 0.2, "temperature": 5200}, "lut": {"name": "warm_film", "intensity": 0.6},
 "effects": [{"type": "punch_in", "start": 1.0, "end": 2.0, "scale": 1.15}],
 "titles": [{"text": "Jane Doe", "subtitle": "Engineer", "style": "lower_third", "start": 0.5, "end": 4, "anim": "slide"}],
 "pip": {"src": "/media/broll.mp4", "in": 30, "full": true, "start": 1.0, "end": 3.5},
 "opacity": 1.0,
 "keyframes": {"x": [[0, 0.3], [4, 0.7, "ease"]], "scale": [[0, 1], [6, 1.2]], "volume": [[0, 1], [5, 0.2]],
               "opacity": [[0, 0], [0.5, 1]], "rotation": [[0, 0], [2, 8]]},
 "rotation_fill": "zoom",
 "perspective": {"tl": [0.1, 0.05], "tr": [0.92, 0.0], "bl": [0.0, 1.0], "br": [1.0, 0.97]},
 "mask": {"shape": "ellipse", "x": 0.4, "y": 0.2, "w": 0.15, "h": 0.25, "effect": "blur", "region": "inside",
          "keyframes": {"x": [[0, 0.40], [3, 0.55]], "y": [[0, 0.2], [3, 0.25]]}},
 "split": {"srcs": ["/media/b.mp4", "/media/c.mp4"], "layout": "main_left", "gap": 6},
 "interpolate": "mci", "audio_lead": 0.4, "audio_tail": 0.6, "freeze": {"at": 2.0, "duration": 1.5}}
```
New keys in this work package:
- `opacity` (0..1) and `keyframes.opacity` - the clip over black (fade through black without a transition).
- `keyframes.rotation` (degrees, clockwise) + `rotation_fill`: `zoom` (default: scaled so no corner shows at the
  largest angle) or `black` (whole picture, black corners).
- `perspective` - keystone/perspective correction: the four corners (0..1 of the fitted frame) of the area that should
  fill the frame (e.g. a slide filmed at an angle).
- `mask.keyframes` - `x`, `y`, `w`, `h` of the mask box over clip time (moving/tracked mask). Build them from a face
  track: `nexus.fx.post.mask_from_track(analysis["faces"]["tracks"][0]["samples"], clip, W, H, src_w, src_h,
  effect="pixelate", region="inside")`. Effects: `blur`, `pixelate`, `dim`, `black`; `region` `inside` (hide the
  face) or `outside` (spotlight the face).
- `split.layout` - besides `h`, `v`, `grid`: `main_left`, `main_right`, `main_top` (the clip as a 2/3 main panel,
  the other sources beside/under it), `thirds_h`, `thirds_v`.

### Layers (`tracks`)
```json
"tracks": [
 {"id": "V2", "kind": "video", "z": 1, "opacity": 1.0, "grade": "bw", "effects": [{"type": "vignette"}],
  "items": [{"id": "l1", "type": "video", "src": "/media/screen.mp4", "in": 5, "out": 11, "start": 3.0,
             "x": 0.55, "y": 0.06, "w": 0.4, "h": 0.4, "opacity": 0.9,
             "keyframes": {"opacity": [[0, 0], [0.4, 1]], "pos_x": [[0, 0.55], [2, 0.05, "ease"]]}}]},
 {"id": "T1", "kind": "text", "z": 2,
  "items": [{"text": "Chapter 2", "style": "title", "start": 10, "end": 13, "anim": "rise"}]},
 {"id": "A2", "kind": "audio", "volume": 0.8, "role": "music",
  "items": [{"src": "/media/sting.wav", "start": 9.5, "in": 0, "out": 2.0, "fade_out": 0.5}]}]
```
- The main `clips` are layer 0 and define the programme length; layers draw in ascending `z` (equal `z`: list order).
- Video items: `start` (programme time), box `x`, `y`, `w`, `h` (0..1 of the frame, default full frame), every clip key
  (effects, grade, mask, titles, keyframes x/y/scale/rotation work inside the box), `opacity`, keyframes `opacity`,
  `pos_x`, `pos_y` (item time). `use_audio: true` mixes the item's own sound (default: silent).
- Track stack: a track's `effects` are appended to every item's; `grade`, `correct`, `lut`, `chroma_key`, `mask`,
  `stabilize` are defaults for items that have none. Track `opacity`/`keyframes.opacity` (programme time) multiply.
  `hidden: true` hides a video/text track; `mute: true` silences an audio track.
- Text items: `text`, `subtitle`, `style` (`title`, `lower_third`, `subtitle`, `callout`, `kinetic`, `caption`),
  `start`, `end`, `anim` (`fade`, `slide`, `rise`, `none`), `from` (`left`/`right`), `opacity`, keyframes `opacity`.
- Audio items: `src`, `start`, `in`, `out`, `volume`, `role` (`music` default; `dialogue`/`narration` duck the music),
  `fade_in`, `fade_out`.
- Ops: `add_track`, `remove_track`, `set_track`, `add_item`, `remove_item`, `move_item(item, start=, track_id=)`,
  `set_item`, `set_keyframes(target_id, prop, points)` (target = clip, item or track id).
- `tracks.validate(tl)` lists problems for the UI (missing sources, overlapping items on one track, bad kinds).

### Interchange
- `nexus.timeline.edl.write(tl, "cut.edl")` / `edl.read("cut.edl", reels={"REEL": path})` - CMX3600, cuts,
  dissolves (`D`), wipes (`W001`-`W004`), speed (`M2`); NEXUS comments make a NEXUS round trip lossless.
- `edl.write(tl, "cut.otio")` / `edl.read("cut.otio")` - OpenTimelineIO JSON (main storyline, transitions, speed).

### Interrupted renders
`render()` leaves `workdir/checkpoint_<name>.json`; run the same render again and it reuses every finished segment /
piece / audio mix (`stats["resumed"]`, `stats["checkpoint"]["reused"]`). For a remote worker,
`nexus.timeline.resume.execution_result(request_id, stats=..., error=..., checkpoint=...)` builds the
`RenderExecutionResult` for Account 1.

## Director: explainable edits (agent B)
- `POST /api/projects/{id}/direct` with `profile` (14 editorial profiles from `GET /api/director/profiles`: hook window,
  median shot length, pattern-interrupt interval, cutaways/min, title style, end structure; legacy names are aliases),
  `target_seconds`, `title`, `instructions`, `broll`, `end_text` (only your words), `profile_overrides` (any profile
  parameter), `reference_profile_id` and `overrides`:
  `{"keep": [decision ids], "drop": [...], "opening": id, "no_lift": true, "reject_cutaways": [...]}`.
- `GET /api/projects/{id}/plan`: every decision with `id`, `confidence` (0-1), `reason`, `text`. Decisions under 0.55 are
  highlighted in the editor; each row has Keep / Drop / Use as opening / Reject cutaway, applied on the next "Direct".
- What it does on real material (docs/results/a2_director/): openings never start on "Yes,"/"And"/a pronoun with no
  referent (standalone lines are moved to the front), endings never on a question or fragment, mid-sentence diarisation
  flips repaired, duplicate takes collapsed to the best one, nothing padded when the source is short.

## B-roll (agent B)
- Ranked by what the picture shows (SigLIP image-text similarity, Apache-2.0, 213 MB opt-in install from the editor),
  plus a small keyword bonus; penalties for imagery already used, for the speaker's own look and for repeating the same
  shot type/motion. Licence gate first; talking heads, studio shots and text slates are rejected as cutaways.
- Candidates: NASA library, Openverse (CC), and your own project files with declared rights (`owned`, `licensed`,
  `public-domain`, `generated`): their low-speech shots become local candidates.
- Measured on a hand-labelled set (28 sentences, 18 inserts, one non-blind labeller): strict 28% -> 56%, lenient 56% -> 94%.
  Demo 1 still showed one off-topic NASA result (Houston infrared under "carbon dioxide or water"): check cutaways.

## Reference style (agent B + integration)
- `POST /api/projects/{id}/reference {media_id, rights_basis}` (owned / licensed / public-domain / commentary; anything
  else is refused) measures STRUCTURE only: shot-length statistics, cuts/min, first 3 s / 10 s, hook timing, shot-type
  changes, visual density, cutaways/min, on-screen text share (edge heuristic, not OCR), speech/music-bed share, audio
  accents/min, pattern-interrupt spacing, ending kind, colour (luma, contrast, saturation, colourfulness, warmth,
  monochrome share) and transitions (hard cut / gradual / fade through black, measured at 12 fps around each cut).
- Pass the returned `reference_profile_id` to Direct: the numbers become profile overrides (shot lengths, pacing, hook
  window, cutaway rate, caption style, music on/off, grade, crossfade policy, ending). No frame, word or sound of the
  reference is used. Demo 4 compares the reference with the rendered result metric by metric.

## Shorts (agent B)
- `POST /api/projects/{id}/shorts`: moments that open on a question or a standalone line and end on a finished
  statement; a context line is added when a short opens on an answer; vertical crop follows the recorded face position;
  captions are placed inside the platform's safe zone. Distinctness on six benchmarks: max pairwise source overlap 0.6%.
- Titles: deterministic titles are complete thoughts only about half the time (Demo 2: "One of the things that caves");
  edit them before publishing. Optional local-LLM titles are checked against the clip's own words.

## Audio (agent C)
- Per clip `audio_fx: "dialogue_v2_ffmpeg"` (editor: Dialogue clean-up) or programme-wide; per item `gain_db`, `pan`
  (-1..1, constant power); music `duck: "keyframes"` dips the bed under caption/narration spans without sidechain pumping.
- Loudness: programme normalised to `target_lufs` (default -14), true peak <= -1 dBTP. A/V sync measured per clip.
- Listening sheet for a person: docs/results/a2_audio_review/REVIEW.md. **No human has approved any audio yet.**

## Enhancement and VFX (agent D)
- `POST /api/projects/{id}/clips/{clip}/process {op, params}` (ops from `GET /api/effects`): upscale (lanczos / Real-ESRGAN:
  invents plausible detail, never "recovers" it), frame interpolation, denoise, deblock, film look, HDR tone map,
  background replacement (MODNet/U2-Net/MediaPipe, consent-gated downloads), tracked mask, particles, depth of field.
  The clip keeps `enhanced_from`, so Undo restores the original.

## Remote rendering (Account 1 workers)
- Timelines of plain cuts can be compiled for Account 1's CPU workers (`nexus/timeline/remote_render.py`, task
  `a2_render_steps`); anything with effects, layers, captions or music renders locally. See ACCOUNT2_HANDOFF_TO_ACCOUNT1.md.
