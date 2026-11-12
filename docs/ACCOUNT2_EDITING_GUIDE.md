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
