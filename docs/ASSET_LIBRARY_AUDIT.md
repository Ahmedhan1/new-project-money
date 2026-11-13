# Asset library audit (Account 2, follow-up to PR #6)

Branch `work/account-2-asset-library-storage`, created from the accepted Account 2 head `d979df1`. PR #6 is unchanged.

## 1. What already existed (reused, not rebuilt)
| Area | Existing (PR #6) | Reused how |
|---|---|---|
| Transitions | 58 FFmpeg `xfade` transitions verified on FFmpeg 6.1.1 + aliases (`nexus/fx/transitions.py`) | all 58 are catalog entries; extended with 18 custom-expression transitions through the same `resolve()` and one new `filter_for()` used by both render pipelines |
| Clip effects | punch_in, zoom, shake, vignette, grain, letterbox, blur, flash, glitch, speed_ramp (`nexus/fx/effects.py`) | catalog entries; 11 finishing looks added to the same `video_filters()` chain |
| Grades / LUTs | 8 grades, 5 built-in .cube LUTs, `.cube` validation, intensity mix (`nexus/fx/post.py`, `nexus/fx/lut.py`) | library LUTs are written with `lut.write_cube`, validated with `lut.validate`, applied through the existing `lut` field |
| Titles / credits | animated PNG titles (fade/slide/rise), rolling credits (`nexus/fx/titles.py`) | templates use them; 4 motion presets added (pop, spring, bounce, zoom) + per-title font |
| Captions | classic, boxed, pop (word-by-word), karaoke (`nexus/fx/captions.py`) | motion presets point at them |
| Programme overlays | progress bar, watermark | new `image` overlay type for frames/borders/layouts (cache key includes file size/mtime) |
| Sound | 6 synthesised SFX (`nexus/fx/sfx.py`), 4 ambiences (`nexus/audio/design.py`), 5 music moods (`nexus/audio/music.py`) | wrapped as catalog assets; new cinematic/UI sounds and 4 nature ambiences added in the same style |
| VFX / enhance | particles, depth of field, tracking, background replacement, film look, upscale, denoise | catalog entries point at the processors (apply = "Process this clip") |
| Licensed stock pictures | `nexus/assets.py` (Openverse/NASA/Commons, licence per file) | kept as is, shown on the Asset Library page |
| Storage | backup backends (`nexus/storage`), Account 1 cloud storage (PR #4) | archive uses Account 1; no second router |

## 2. What was missing and is now implemented
- **Catalog + manager** (`nexus/assetlib`, `nexus/registry/asset_library.yaml`): 182 assets with stable id, name,
  category, description, preview, parameters, source URL, author, licence, commercial-use flag, attribution, SHA-256 and
  installation status; manifest-driven lazy install; SHA-256 verification; duplicate detection; versioned install
  records; licence gate; safe import / pack export / pack import; cache quota with LRU; preview caching; missing-asset
  detection and repair; apply to clip / to timeline (undoable versions).
- **Effects**: glow, bloom, motion blur, chromatic aberration, light leak, lens distortion, VHS, film burn, sharpen,
  denoise, professional finish.
- **Transitions**: spin, whip pan (left/right), zoom blur, blur dissolve, glitch slices, film burn, 6 luma wipes
  (radial, clouds, diagonal, clock, ripple, bars), 5 shapes (diamond, star, heart, cross, hexagon). Mask-based wipes are
  expressed as luma/shape threshold maps inside FFmpeg's custom xfade (a user-supplied mask *image* as a third input is
  not supported by xfade and is not built). Automatic fallback to the nearest built-in transition on FFmpeg < 6.
- **LUTs**: 14 generated looks (cinematic, warm film, cool cinematic, documentary, vintage, black & white, luxury
  commercial, natural skin, teal & orange, soft contrast, high contrast, bleach bypass, day for night, sepia), each with a
  before/after preview rendered through `lut3d`.
- **Frames / overlays / titles / templates**: cinematic borders 2.39 and 1.85, 35 mm film frame, viewfinder, vignette
  frame, lower-third bar, documentary location tag, callout circle/arrow/box, social 9:16 safe-zone layout, split divider,
  typography layout; templates for lower thirds, title card, chapter card, quote card, end card, credits roll, social
  vertical.
- **Motion graphics**: Ken Burns in/out, camera push/pull, pan drift, animated emphasis, title pop/spring/bounce/
  fade-and-scale/slide/rise, word-by-word and karaoke captions. True multi-plane parallax needs a depth split and is not
  built (pan drift is labelled as such).
- **Audio**: whoosh, reverse whoosh, impact, cinematic hit, braam, sub drop, riser, swell, glitch stutter, camera
  shutter, typewriter, UI click/pop/notification/swipe; room tone, outdoor, city, synthetic birds, rain, wind, ocean,
  stream; documentary/calm/tense/inspiring/upbeat beds; the CC0 "50 retro/synth SFX" pack (rubberduck, OpenGameArt).
- **Fonts** (OFL 1.1, pinned to google/fonts commit 3e56677, licence text kept): Bebas Neue, Montserrat, Oswald,
  Playfair Display, Anton, Inter, Noto Sans Arabic.
- **Disk management** (`nexus/diskman.py`), **daily archive** (`nexus/archive.py`), Storage-page dashboards, CLI.

## 3. Framework and library evaluation
No new Python or system dependency was installed. Everything above uses what the project already ships.
| Tool | Already present? | Needed? | Licence | Windows / Linux | RAM / disk | Decision |
|---|---|---|---|---|---|---|
| FFmpeg / FFprobe | yes (core) | yes | LGPL/GPL build | winget/choco · apt | small | **used**: every effect/transition/LUT/overlay is an FFmpeg filter. Custom transitions need FFmpeg >= 6 (`a0(x,y)`), detected at runtime |
| OpenTimelineIO | yes (0.18, EDL/OTIO export) | no new use | Apache-2.0 | both | small | unchanged |
| OpenCV (headless) | yes (`requirements-ci.txt`) | no new use | Apache-2.0 | both | ~60 MB | unchanged (tracking/VFX) |
| PyAV | present as a transitive dependency (19.0) | no | BSD | both | ~40 MB | **not added**: FFmpeg CLI already covers decode/encode |
| Pillow | yes | yes | MIT-CMU | both | small | **used**: overlays, frames, previews, font specimens |
| NumPy / SciPy | yes | yes | BSD | both | existing | **used**: sound synthesis and filters |
| ImageMagick | system binary on this container only | no | Apache-2.0 | separate install on Windows | ~50 MB | **not used**: Pillow covers generation; avoids a second Windows install |
| Blender Python API | optional, already detected by `nexus/animation` | no | GPL | 300+ MB install | heavy | **not used** for the library (too heavy for presets) |
| G'MIC | no | no | CeCILL | extra binary | moderate | **not added**: FFmpeg filters cover the looks; CeCILL licence review not worth it |
| VapourSynth | no | no | LGPL | needs Python plugin builds on Windows | moderate | **not added**: no gain over FFmpeg filters for these effects |
| Existing audio tools (scipy filters, noisereduce, FFmpeg loudnorm) | yes | yes | BSD/MIT | both | existing | **used** |

## 4. Measured
- Catalog: 182 assets; licence check 182/182 pass (`docs/ASSET_LICENSES.md`).
- Everything installed (all 60 generated + 7 fonts + the pack) in a clean home: 67 assets, **116 MB**
  (music beds 52 MB, ambiences 44 MB, LUTs 13 MB, sounds 3.8 MB, pack 1.7 MB, fonts 1.2 MB, overlays/templates 0.2 MB)
  plus 3.1 MB of cached previews. Installing + previewing every built-in and generated asset took ~141 s on the 4-vCPU
  container (one-time). Most users install a handful; nothing is installed by default.
- Effects, transitions, LUTs, overlays, sounds and title motion change rendered pixels/audio (tests in
  `tests/fast/test_a2_assetlib.py`); contact sheets inspected (`docs/results/a2_assets/`).
