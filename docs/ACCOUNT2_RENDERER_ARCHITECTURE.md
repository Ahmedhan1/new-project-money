# Account 2 renderer architecture (agent A)

Code: `nexus/timeline/render.py` (entry point, segments, legacy path, mix graph), `pieces.py` (piece pipeline),
`tracks.py` (multi-track compiler + compositor), `resume.py` (checkpoints), `edl.py` (EDL/OTIO), `ops.py` (edits),
`nexus/fx/*` (filter builders). Schema: `docs/TIMELINE.md`. Examples: `docs/ACCOUNT2_EDITING_GUIDE.md`.
Benchmark: `scripts/benchmarks/render_bench.py` -> `docs/results/a2_render_bench.json`.

## Pipeline
```
timeline dict
  |  _jl_cuts (J/L-cut audio -> extra dialogue items)   tracks.with_audio (audio tracks -> tl.audio)
  |  tracks.plan (video/text layers, z order, boxes)    resume.Checkpoint (workdir/checkpoint_<out>.json)
  v
STAGE 1  segments  (thread pool, `workers` FFmpeg processes, threads split between them)
  every main clip AND every video-layer item -> cache/segments/seg_<segment_key>.mp4
  (H.264 + AAC, exact frame count round(duration*fps), audio padded/trimmed to the picture)
  v
STAGE 2  pieces + audio, in parallel                                  [pipeline "pieces", default]
  pieces.plan: per clip an optional transition piece (tail of clip i-1 + head of clip i, xfade) and a body piece
  each piece: decode the frames it needs from the segments (-ss + trim, frame exact), composite layers
  (tracks.layer_graph), burn captions (ASS on the programme clock) and overlays -> cache/pieces/piece_<key>.mp4
  a body with nothing burned and no transition = stream copy of its segment (bit-identical)
  audio thread: segments' audio -> acrossfade/concat (sample exact) -> mix (narration, ducked music, sfx, audio
  tracks) -> two-pass loudnorm -> AAC -> cache/pieces/audio_<key>.m4a
  v
STAGE 3  final mux: concat demuxer stream copy of the pieces + the cached audio -> <out>.partial.mp4 -> rename
```
Legacy path (`render_pipeline: "legacy"`): segments -> concat (stream copy) or one xfade graph over every segment
(full re-encode) -> mix + captions + overlays + layers in one final pass (second full re-encode when anything is burned).

## Cache keys (all content hashes; a changed input can never be served a stale file)
| Artifact | Key contains |
|---|---|
| segment | every clip key except `id`/`label`/`notes`/`transition*`; width, height, fps, grade, lang; audio fx chain (+ model signature for `clean_speech`); LUT file content; draft flag; slowmo profile; size+mtime of `src` and `pip.src`. Unknown keys are included on purpose. Layer items: the item clip after the track stack is merged, at its box size. |
| piece | kind, the segment keys and first frames it reads, frame count, xfade name, encoder settings ("copy" for stream-copied bodies), and what is burned into it: cues overlapping it + caption style + language, overlays + programme offset/length, layer signatures (item, track, segment, box) visible in it. |
| audio | segment keys, frame layout (frames, transition frames), fps, every audio item with its source signature (J/L-cut WAVs by name+size), music + signature, target LUFS, AAC settings. |
| overlays/masks | PNG/raw mattes in cache/overlays by their parameters (text, style, size, mask geometry/keyframes). |

Consequences: a caption edit re-encodes only the pieces under the edited cue; a clip edit re-renders that clip's
segment and its pieces (plus the audio mix); a warm render is a stream copy. Draft renders use separate caches
(`segments` with a draft key, `pieces_draft`).

## Resume model
- Every cached file is written as `<name>.<thread>.tmp.*` and renamed when complete; a kill loses only files in flight.
- `checkpoint_<out>.json` (shape of `RenderExecutionResult.checkpoint`): `timeline_sha`, `stage`
  (`segments`/`pieces`/`final`/`done`), `stages_done`, `segments_total/done`, `pieces_total/done`, `audio_done`,
  `attempt`, `resumed`, `previous` (what the interrupted attempt had finished), `reused` counts.
- A retry of the same timeline (same `NEXUS_HOME`, or a worker given the cache and the checkpoint via `checkpoint=`)
  reuses every finished segment/piece/audio file; stale tmp files older than 15 min are deleted.
- The output is written to `<out>.partial.mp4` and renamed: a killed render never leaves a truncated programme.
- `resume.execution_result()` builds the contract document (`done`, or `failed` + `retryable` + checkpoint).

## Benchmark (shared, noisy 4-vCPU container; see a2_render_bench.json for every run)
Timeline: 16 clips from interview + explainer + broll (licensed benchmark media), 64.5 s at 1280x720/30, 3 crossfades,
4 cutaways (full-frame + PiP), animated title + lower third, 24 boxed caption cues, ducked music bed. Wall seconds.
`legacy_rerun` and `pieces_2`/`pieces_audio_digest` ran back to back under similar load; the first two rows ran
under heavier load (load average 7-12). One-clip edit = grade of one clip; caption edit = text of one cue.

| run | cold s | warm s | one-clip edit s | caption edit s | draft s | cold CPU (of 4) | cold peak RSS MB | cache MB |
|---|---|---|---|---|---|---|---|---|
| baseline_c1e2d90 | 111.2 | 79.1 | 67.6 | 74.8 | 55.3 | 0.33 | 852 | 52 |
| pieces_1 | 68.2 | 0.5 | 19.5 | 4.4 | 37.5 | 0.38 | 506 | 54 |
| legacy_rerun | 50.9 | 30.9 | 35.0 | 33.6 | 29.4 | 0.71 | 853 | 52 |
| pieces_2 | 33.7 | 0.4 | 12.1 | 2.5 | 20.1 | 0.76 | 503 | 54 |
| pieces_audio_digest | 37.5 | 0.4 | 3.9 | 2.0 | 22.5 | 0.75 | 501 | 54 |

SLOWMO_TABLE

## Limits (honest)
- Layers are drawn over the main storyline only; a layer cannot sit below it. Layer items on the same track may overlap
  (validate() reports it); both are drawn in list order.
- Per-piece caption burning re-encodes every piece a cue touches; with captions over the whole programme a cold render
  still encodes every frame twice (segment + piece). Pieces never re-encode a third time.
- xfade `dissolve` (and other noise-based transitions) are random per encode, so a re-rendered transition piece is not
  bit-identical to an earlier one (visually equivalent).
- Opacity of a main clip composites over black, not over the previous clip (use a crossfade or a layer for that).
- Text layers use the title renderer (Pillow PNGs): no per-character animation; captions remain the ASS path.
- EDL export covers the main storyline (one video track); layers are not in the EDL. OTIO export writes one track.
- Optical-flow slow motion (minterpolate mci) is CPU-heavy; it is opt-in only (`slowmo: optical_flow` or
  `interpolate: mci`); the last ~0.25 s of an interpolated clip holds its final frame.
- Hardware encoders are used only when `media.encoders` proved one works; none exists in this container, so every
  number above is libx264 on CPU.
