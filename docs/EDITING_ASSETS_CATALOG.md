# Editing assets catalog

Generated from `nexus/registry/asset_library.yaml` + `nexus/fx/transitions.py` by
`scripts/maintenance/gen_asset_docs.py`. Do not edit by hand.

**182 assets**: animations 14, effects 21, fonts 7, frames 5, luts 14, music 5, overlays 7, sound_effects 24, templates 3, titles 6, transitions 76.

Kinds: **builtin** = a renderer feature (nothing to download); **generated** = made locally on install (own work, CC0);
**download** = pinned URL + SHA-256, fetched on demand; **pack** = pinned archive, selected members extracted.

## animations (14)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `motion.camera_pull` | Camera pull | builtin |  | Subtle dolly-out. | {'scale': [[0, 1.08, 'ease'], [-1, 1.0, 'ease']]} |
| `motion.camera_push` | Camera push | builtin |  | Subtle dolly-in feel on interviews. | {'scale': [[0, 1.0, 'ease'], [-1, 1.08, 'ease']]} |
| `motion.captions_karaoke` | Karaoke captions | builtin |  | Whole line, words light up as spoken. | {'style': 'karaoke'} |
| `motion.captions_word_by_word` | Word-by-word captions | builtin |  | 2-4 words per cue, scale-in pop, current word highlighted. | {'style': 'pop', 'words_per_cue': 3} |
| `motion.emphasis_punch` | Animated emphasis | builtin |  | Punch-in on the beat for emphasis. | {'type': 'punch_in'} |
| `motion.ken_burns_in` | Ken Burns in | builtin |  | Slow push into a still or a shot. | {'scale': [[0, 1.0, 'ease'], [-1, 1.15, 'ease']]} |
| `motion.ken_burns_out` | Ken Burns out | builtin |  | Slow pull back. | {'scale': [[0, 1.15, 'ease'], [-1, 1.0, 'ease']]} |
| `motion.pan_drift` | Pan drift (parallax-style) | builtin |  | Lateral drift across a zoomed frame. True multi-plane parallax needs a depth split (not built). | {'scale': [[0, 1.12, 'linear'], [-1, 1.12, 'linear']], 'x': [[0, 0.3, 'ease'], [-1, 0.7, 'ease']]} |
| `motion.title_bounce` | Title bounce | builtin |  | Falls in and bounces. |  |
| `motion.title_fade_scale` | Fade and scale | builtin |  | Fade while scaling 0.85 to 1. |  |
| `motion.title_pop` | Title pop (bounce in) | builtin |  | Scale-in with overshoot. |  |
| `motion.title_rise` | Title rise (text reveal) | builtin |  | Rises into place while fading in. |  |
| `motion.title_slide` | Title slide | builtin |  | Slides in from the side. |  |
| `motion.title_spring` | Title spring | builtin |  | Damped spring settle. |  |

## effects (21)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `effect.bloom` | Bloom | builtin |  | Only highlights above a threshold bleed light, like lens bloom. | amount: 0..1 (0.45); threshold: 0..255 luma (175); radius: px (14) |
| `effect.blur` | Blur | builtin |  | Gaussian blur for a time window (existing renderer effect). | {'type': 'blur', 'amount': 14} |
| `effect.camera_shake` | Camera shake | builtin |  | Handheld jitter (existing renderer effect). | {'type': 'shake'} |
| `effect.chromatic_aberration` | Chromatic aberration | builtin |  | Red/blue fringes from an imperfect lens. | amount: px (3) |
| `effect.denoise` | Denoise | builtin |  | Spatio-temporal noise reduction (hqdn3d). | strength: 0..10 (3) |
| `effect.depth_of_field` | Depth of field | builtin |  | Background blur from a depth estimate (VFX processor; runs on a processed copy of the clip). |  |
| `effect.film_burn` | Film burn | builtin |  | Overexposed orange burn near the end of the clip. | start: s; duration: s (1.2); color: #RRGGBB (#FF7A1A) |
| `effect.film_grain` | Film grain | builtin |  | Temporal luma grain like 16/35 mm stock. | {'type': 'grain', 'amount': 10} |
| `effect.finish` | Professional finish | builtin |  | Light sharpening, fine grain and a subtle vignette in one step. | amount: 0..1 (0.5) |
| `effect.glitch` | Glitch | builtin |  | Short RGB split + noise bursts (existing renderer effect). | {'type': 'glitch', 'amount': 10} |
| `effect.glow` | Glow | builtin |  | Soft screen-blended halo over the whole picture. | amount: 0..1 (0.35); radius: px (10) |
| `effect.lens_distortion` | Lens distortion | builtin |  | Barrel distortion of a wide lens. | k1: -0.5..0.5 (-0.15 barrel); k2: -0.5..0.5 (0.02) |
| `effect.letterbox` | Letterbox 2.39 | builtin |  | Scope bars drawn into the picture. | {'type': 'letterbox', 'ratio': 2.39} |
| `effect.light_leak` | Light leak | builtin |  | A warm, slowly moving flare washing in from the edge of frame. | amount: 0..1 (0.5); color: #RRGGBB (#FF9A3C); start: s; end: s; speed: 0.001..0.2 (0.02) |
| `effect.light_leak_cool` | Light leak (cool) | builtin |  | Cool cyan variant of the light leak. | amount: 0..1 (0.5); color: #RRGGBB (#FF9A3C); start: s; end: s; speed: 0.001..0.2 (0.02) |
| `effect.motion_blur` | Motion blur | builtin |  | Blends neighbouring frames for a long-shutter look. | frames: 2..8 frames blended (3) |
| `effect.particles` | Particles | builtin |  | Procedural dust/snow/sparks (VFX processor). |  |
| `effect.punch_in` | Punch-in emphasis | builtin |  | Quick zoom-in on a beat (existing renderer effect). | {'type': 'punch_in'} |
| `effect.sharpen` | Sharpen | builtin |  | Unsharp mask; sharpens existing detail (never invents it). | amount: 0..2 (0.6) |
| `effect.vhs` | VHS | builtin |  | Chroma bleed, soft picture, tape noise and scanlines. | amount: 0..1 (0.6) |
| `effect.vignette` | Vignette | builtin |  | Darkened corners that draw the eye to the centre. | {'type': 'vignette'} |

## fonts (7)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `font.anton` | Anton | download |  | Heavy impact display face for social captions. |  |
| `font.bebas_neue` | Bebas Neue | download |  | Tall condensed display caps for titles. |  |
| `font.inter` | Inter | download |  | Neutral UI sans; very readable captions. |  |
| `font.montserrat` | Montserrat | download |  | Geometric sans for lower thirds and captions. |  |
| `font.noto_sans_arabic` | Noto Sans Arabic | download |  | Arabic titles and captions (RTL shaping by libraqm/libass). |  |
| `font.oswald` | Oswald | download |  | Condensed news-style sans. |  |
| `font.playfair_display` | Playfair Display | download |  | High-contrast serif for luxury and documentary titles. |  |

## frames (5)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `frame.cinematic_185` | Cinematic border 1.85 | generated |  | Flat widescreen bars. | {'kind': 'letterbox', 'ratio': 1.85} |
| `frame.cinematic_239` | Cinematic border 2.39 | generated |  | Scope letterbox as an overlay layer. | {'kind': 'letterbox', 'ratio': 2.39} |
| `frame.film_35mm` | 35 mm film frame | generated |  | Sprocket holes and rounded gate. | {'kind': 'film_frame'} |
| `frame.viewfinder` | Camera viewfinder | generated |  | REC dot, corner marks, centre cross, thirds, battery. | {'kind': 'viewfinder'} |
| `frame.vignette` | Vignette frame | generated |  | Dark edge falloff as a layer. | {'kind': 'vignette_frame'} |

## luts (14)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `lut.black_white` | Black and white | generated |  | Panchromatic-weighted monochrome with contrast. | {'look': 'black_white'} |
| `lut.bleach_bypass` | Bleach bypass | generated |  | Silver-retained desaturated contrast. | {'look': 'bleach_bypass'} |
| `lut.cinematic` | Cinematic | generated |  | Warm highlights, cool shadows, gentle S-curve, slightly muted. | {'look': 'cinematic'} |
| `lut.cool_cinematic` | Cool cinematic | generated |  | Blue-steel thriller look. | {'look': 'cool_cinematic'} |
| `lut.day_for_night` | Day for night | generated |  | Dark blue moonlight from daylight footage. | {'look': 'day_for_night'} |
| `lut.documentary` | Documentary | generated |  | Natural, lightly desaturated, lifted blacks. | {'look': 'documentary'} |
| `lut.high_contrast` | High contrast | generated |  | Punchy blacks and whites. | {'look': 'high_contrast'} |
| `lut.luxury_commercial` | Luxury commercial | generated |  | Rich colour, gold-touched highlights. | {'look': 'luxury_commercial'} |
| `lut.natural_skin` | Natural skin tones | generated |  | Protects skin hues while calming greens and blues. | {'look': 'natural_skin'} |
| `lut.sepia` | Sepia | generated |  | Toned monochrome. | {'look': 'sepia'} |
| `lut.soft_contrast` | Soft contrast | generated |  | Low-contrast, gentle roll-off. | {'look': 'soft_contrast'} |
| `lut.teal_orange` | Teal and orange | generated |  | Blockbuster complementary split. | {'look': 'teal_orange'} |
| `lut.vintage` | Vintage | generated |  | Faded, yellowed print. | {'look': 'vintage'} |
| `lut.warm_film` | Warm film | generated |  | Golden film stock warmth. | {'look': 'warm_film'} |

## music (5)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `music.calm_bed` | Calm bed | generated |  | Gentle major pads. | {'mood': 'calm'} |
| `music.documentary_bed` | Documentary bed | generated |  | Slow minor pads for narration. | {'mood': 'documentary'} |
| `music.inspiring_bed` | Inspiring bed | generated |  | Uplifting progression. | {'mood': 'inspiring'} |
| `music.tense_bed` | Tension bed | generated |  | Dark suspense pads. | {'mood': 'tense'} |
| `music.upbeat_bed` | Upbeat bed | generated |  | Faster bright progression. | {'mood': 'upbeat'} |

## overlays (7)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `overlay.callout_arrow` | Callout arrow | generated |  | Pointer arrow. | {'kind': 'callout_arrow'} |
| `overlay.callout_box` | Callout box | generated |  | Rounded highlight box. | {'kind': 'callout_box'} |
| `overlay.callout_circle` | Callout circle | generated |  | Highlight ring. | {'kind': 'callout_circle'} |
| `overlay.location_tag` | Documentary location tag | generated |  | Top-left tag plate for place and date. | {'kind': 'location_tag'} |
| `overlay.lower_third_bar` | Lower third bar | generated |  | Dark bar with an accent stripe for names. | {'kind': 'lower_third_bar'} |
| `overlay.social_safe_zone` | Social vertical layout | generated |  | 9:16 title band and caption band inside the Shorts/TikTok safe area. | {'kind': 'social_safe_zone', 'w': 1080, 'h': 1920} |
| `overlay.split_frame` | Split-screen divider | generated |  | Centre divider for side-by-side layouts. | {'kind': 'split_frame'} |

## sound_effects (24)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `ambience.city` | City hum | generated |  | Distant traffic rumble. | {'kind': 'city'} |
| `ambience.nature_birds` | Nature (synthetic birds) | generated |  | Air with sparse synthesised chirps (not a field recording). | {'kind': 'nature'} |
| `ambience.ocean` | Ocean waves | generated |  | Slow wave wash. | {'kind': 'ocean'} |
| `ambience.outdoor` | Outdoor air | generated |  | Light outdoor bed. | {'kind': 'outdoor'} |
| `ambience.rain` | Rain | generated |  | Steady rain with drops. | {'kind': 'rain'} |
| `ambience.room_tone` | Room tone | generated |  | Quiet interior air to fill cuts. | {'kind': 'room'} |
| `ambience.stream` | Stream | generated |  | Running water. | {'kind': 'stream'} |
| `ambience.wind` | Wind | generated |  | Gusting wind. | {'kind': 'wind'} |
| `sfx.braam` | Braam | generated |  | Low brassy swell for trailers. | {'kind': 'braam'} |
| `sfx.camera_shutter` | Camera shutter | generated |  | Two-blade shutter click. | {'kind': 'camera_shutter'} |
| `sfx.cinematic_hit` | Cinematic hit | generated |  | Boom with transient and long tail. | {'kind': 'cinematic_hit'} |
| `sfx.glitch_stutter` | Glitch stutter | generated |  | Digital stutter for glitch transitions. | {'kind': 'glitch_stutter'} |
| `sfx.impact` | Impact | generated |  | Punchy hit. | {'kind': 'impact'} |
| `sfx.reverse_whoosh` | Reverse whoosh | generated |  | Builds into a cut. | {'kind': 'reverse_whoosh'} |
| `sfx.riser` | Riser | generated |  | Tension build. | {'kind': 'riser'} |
| `sfx.sub_drop` | Sub drop | generated |  | Falling sub-bass. | {'kind': 'sub_drop'} |
| `sfx.swell` | Swell | generated |  | Soft rising pad swell. | {'kind': 'swell'} |
| `sfx.typewriter` | Typewriter | generated |  | Key clicks for text reveals. | {'kind': 'typewriter'} |
| `sfx.ui_click` | UI click | generated |  | Interface click. | {'kind': 'click'} |
| `sfx.ui_notification` | UI notification | generated |  | Two-tone chime. | {'kind': 'ui_notification'} |
| `sfx.ui_pop` | UI pop | generated |  | Bubble pop for captions and icons. | {'kind': 'pop'} |
| `sfx.ui_swipe` | UI swipe | generated |  | Short swipe. | {'kind': 'ui_swipe'} |
| `sfx.whoosh` | Whoosh | generated |  | Air sweep for transitions. | {'kind': 'whoosh'} |
| `sfxpack.retro_synth_50` | 50 retro / synth SFX (pack) | pack |  | Power-ups, coins, lasers, beeps and explosions (UI and game-style sounds). |  |

## templates (3)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `template.lower_third_basic` | Lower third | generated |  | Name + role title on the selected clip. | {'name': 'lower_third_basic'} |
| `template.lower_third_documentary` | Documentary lower third | generated |  | Location/date title with the bar overlay. | {'name': 'lower_third_documentary'} |
| `template.social_vertical` | Social vertical | generated |  | Reframes to 9:16 with word-by-word captions and the safe-zone layout. | {'name': 'social_vertical'} |

## titles (6)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `overlay.typography_grid` | Typography layout | generated |  | Headline, rule and kicker on a grid (edit the text in the template). | {'kind': 'typography_grid'} |
| `template.chapter_card` | Chapter card | generated |  | Black card with chapter title. | {'name': 'chapter_card'} |
| `template.credits_roll` | End credits roll | generated |  | Rolling credits on black. | {'name': 'credits_roll'} |
| `template.end_card` | End card | generated |  | Closing card with a call to action. | {'name': 'end_card'} |
| `template.quote_card` | Quote card | generated |  | Pull-quote card. | {'name': 'quote_card'} |
| `template.title_card_center` | Animated title card | generated |  | Inserts a 3 s title card before the clip. | {'name': 'title_card_center'} |

## transitions (76)

| ID | Name | Kind | Group | Description | Parameters |
|---|---|---|---|---|---|
| `transition.blur_dissolve` | Blur dissolve | builtin | blur | Dissolve through a blur. Custom expression (FFmpeg >= 6); falls back to hblur on older FFmpeg. | {'duration': 0.6} |
| `transition.circleclose` | Circleclose | builtin | shape | FFmpeg xfade 'circleclose' (shape). | {'duration': 0.6} |
| `transition.circlecrop` | Circlecrop | builtin | shape | FFmpeg xfade 'circlecrop' (shape). | {'duration': 0.6} |
| `transition.circleopen` | Circleopen | builtin | shape | FFmpeg xfade 'circleopen' (shape). | {'duration': 0.6} |
| `transition.coverdown` | Coverdown | builtin | push | FFmpeg xfade 'coverdown' (push). | {'duration': 0.6} |
| `transition.coverleft` | Coverleft | builtin | push | FFmpeg xfade 'coverleft' (push). | {'duration': 0.6} |
| `transition.coverright` | Coverright | builtin | push | FFmpeg xfade 'coverright' (push). | {'duration': 0.6} |
| `transition.coverup` | Coverup | builtin | push | FFmpeg xfade 'coverup' (push). | {'duration': 0.6} |
| `transition.diagbl` | Diagbl | builtin | wipe | FFmpeg xfade 'diagbl' (wipe). | {'duration': 0.6} |
| `transition.diagbr` | Diagbr | builtin | wipe | FFmpeg xfade 'diagbr' (wipe). | {'duration': 0.6} |
| `transition.diagtl` | Diagtl | builtin | wipe | FFmpeg xfade 'diagtl' (wipe). | {'duration': 0.6} |
| `transition.diagtr` | Diagtr | builtin | wipe | FFmpeg xfade 'diagtr' (wipe). | {'duration': 0.6} |
| `transition.dissolve` | Dissolve | builtin | dissolve | Noise dissolve. | {'duration': 0.6} |
| `transition.distance` | Distance | builtin | dissolve | Distance-field blend. | {'duration': 0.6} |
| `transition.fade` | Fade | builtin | dissolve | Cross-dissolve. | {'duration': 0.6} |
| `transition.fadeblack` | Fadeblack | builtin | dissolve | Dip to black. | {'duration': 0.6} |
| `transition.fadefast` | Fadefast | builtin | dissolve | Fast crossfade. | {'duration': 0.6} |
| `transition.fadegrays` | Fadegrays | builtin | dissolve | Fade through greyscale. | {'duration': 0.6} |
| `transition.fadeslow` | Fadeslow | builtin | dissolve | Slow crossfade. | {'duration': 0.6} |
| `transition.fadewhite` | Fadewhite | builtin | dissolve | Dip to white (flash). | {'duration': 0.6} |
| `transition.film_burn` | Film burn | builtin | film burn | Organic burn-through with a hot orange edge. Custom expression (FFmpeg >= 6); falls back to fadewhite on older FFmpeg. | {'duration': 0.6} |
| `transition.glitch_slices` | Glitch slices | builtin | glitch | Displaced horizontal slices with RGB split. Custom expression (FFmpeg >= 6); falls back to pixelize on older FFmpeg. | {'duration': 0.6} |
| `transition.hblur` | Hblur | builtin | blur | Blur through. | {'duration': 0.6} |
| `transition.hlslice` | Hlslice | builtin | wipe | FFmpeg xfade 'hlslice' (wipe). | {'duration': 0.6} |
| `transition.hlwind` | Hlwind | builtin | wipe | FFmpeg xfade 'hlwind' (wipe). | {'duration': 0.6} |
| `transition.horzclose` | Horzclose | builtin | wipe | FFmpeg xfade 'horzclose' (wipe). | {'duration': 0.6} |
| `transition.horzopen` | Horzopen | builtin | wipe | FFmpeg xfade 'horzopen' (wipe). | {'duration': 0.6} |
| `transition.hrslice` | Hrslice | builtin | wipe | FFmpeg xfade 'hrslice' (wipe). | {'duration': 0.6} |
| `transition.hrwind` | Hrwind | builtin | wipe | FFmpeg xfade 'hrwind' (wipe). | {'duration': 0.6} |
| `transition.luma_bars` | Luma bars | builtin | luma | Wavy vertical bars. Custom expression (FFmpeg >= 6); falls back to hlslice on older FFmpeg. | {'duration': 0.6} |
| `transition.luma_clock` | Luma clock | builtin | luma | Clock-hand sweep. Custom expression (FFmpeg >= 6); falls back to radial on older FFmpeg. | {'duration': 0.6} |
| `transition.luma_clouds` | Luma clouds | builtin | luma | Cloud-shaped luma wipe. Custom expression (FFmpeg >= 6); falls back to dissolve on older FFmpeg. | {'duration': 0.6} |
| `transition.luma_diagonal` | Luma diagonal | builtin | luma | Soft diagonal luma wipe. Custom expression (FFmpeg >= 6); falls back to diagbr on older FFmpeg. | {'duration': 0.6} |
| `transition.luma_radial` | Luma radial | builtin | luma | Soft radial luma wipe. Custom expression (FFmpeg >= 6); falls back to circleopen on older FFmpeg. | {'duration': 0.6} |
| `transition.luma_ripple` | Luma ripple | builtin | luma | Rippling circular luma wipe. Custom expression (FFmpeg >= 6); falls back to circleopen on older FFmpeg. | {'duration': 0.6} |
| `transition.pixelize` | Pixelize | builtin | glitch | Pixelate out and in (glitch). | {'duration': 0.6} |
| `transition.radial` | Radial | builtin | shape | FFmpeg xfade 'radial' (shape). | {'duration': 0.6} |
| `transition.rectcrop` | Rectcrop | builtin | shape | FFmpeg xfade 'rectcrop' (shape). | {'duration': 0.6} |
| `transition.revealdown` | Revealdown | builtin | push | FFmpeg xfade 'revealdown' (push). | {'duration': 0.6} |
| `transition.revealleft` | Revealleft | builtin | push | FFmpeg xfade 'revealleft' (push). | {'duration': 0.6} |
| `transition.revealright` | Revealright | builtin | push | FFmpeg xfade 'revealright' (push). | {'duration': 0.6} |
| `transition.revealup` | Revealup | builtin | push | FFmpeg xfade 'revealup' (push). | {'duration': 0.6} |
| `transition.shape_cross` | Shape cross | builtin | shape | Cross iris. Custom expression (FFmpeg >= 6); falls back to vertopen on older FFmpeg. | {'duration': 0.6} |
| `transition.shape_diamond` | Shape diamond | builtin | shape | Diamond iris. Custom expression (FFmpeg >= 6); falls back to circleopen on older FFmpeg. | {'duration': 0.6} |
| `transition.shape_heart` | Shape heart | builtin | shape | Heart iris. Custom expression (FFmpeg >= 6); falls back to circleopen on older FFmpeg. | {'duration': 0.6} |
| `transition.shape_hexagon` | Shape hexagon | builtin | shape | Hexagon iris. Custom expression (FFmpeg >= 6); falls back to circleopen on older FFmpeg. | {'duration': 0.6} |
| `transition.shape_star` | Shape star | builtin | shape | Star iris. Custom expression (FFmpeg >= 6); falls back to circleopen on older FFmpeg. | {'duration': 0.6} |
| `transition.slidedown` | Slidedown | builtin | slide | FFmpeg xfade 'slidedown' (slide). | {'duration': 0.6} |
| `transition.slideleft` | Slideleft | builtin | slide | FFmpeg xfade 'slideleft' (slide). | {'duration': 0.6} |
| `transition.slideright` | Slideright | builtin | slide | FFmpeg xfade 'slideright' (slide). | {'duration': 0.6} |
| `transition.slideup` | Slideup | builtin | slide | FFmpeg xfade 'slideup' (slide). | {'duration': 0.6} |
| `transition.smoothdown` | Smoothdown | builtin | slide | FFmpeg xfade 'smoothdown' (slide). | {'duration': 0.6} |
| `transition.smoothleft` | Smoothleft | builtin | slide | FFmpeg xfade 'smoothleft' (slide). | {'duration': 0.6} |
| `transition.smoothright` | Smoothright | builtin | slide | FFmpeg xfade 'smoothright' (slide). | {'duration': 0.6} |
| `transition.smoothup` | Smoothup | builtin | slide | FFmpeg xfade 'smoothup' (slide). | {'duration': 0.6} |
| `transition.spin` | Spin | builtin | spin | Spins and shrinks out, the next shot spins in. Custom expression (FFmpeg >= 6); falls back to radial on older FFmpeg. | {'duration': 0.6} |
| `transition.squeezeh` | Squeezeh | builtin | zoom | FFmpeg xfade 'squeezeh' (zoom). | {'duration': 0.6} |
| `transition.squeezev` | Squeezev | builtin | zoom | FFmpeg xfade 'squeezev' (zoom). | {'duration': 0.6} |
| `transition.vdslice` | Vdslice | builtin | wipe | FFmpeg xfade 'vdslice' (wipe). | {'duration': 0.6} |
| `transition.vdwind` | Vdwind | builtin | wipe | FFmpeg xfade 'vdwind' (wipe). | {'duration': 0.6} |
| `transition.vertclose` | Vertclose | builtin | wipe | FFmpeg xfade 'vertclose' (wipe). | {'duration': 0.6} |
| `transition.vertopen` | Vertopen | builtin | wipe | FFmpeg xfade 'vertopen' (wipe). | {'duration': 0.6} |
| `transition.vuslice` | Vuslice | builtin | wipe | FFmpeg xfade 'vuslice' (wipe). | {'duration': 0.6} |
| `transition.vuwind` | Vuwind | builtin | wipe | FFmpeg xfade 'vuwind' (wipe). | {'duration': 0.6} |
| `transition.whip_pan_left` | Whip pan left | builtin | whip pan | Fast motion-blurred pan to the left. Custom expression (FFmpeg >= 6); falls back to slideleft on older FFmpeg. | {'duration': 0.6} |
| `transition.whip_pan_right` | Whip pan right | builtin | whip pan | Fast motion-blurred pan to the right. Custom expression (FFmpeg >= 6); falls back to slideright on older FFmpeg. | {'duration': 0.6} |
| `transition.wipebl` | Wipebl | builtin | wipe | FFmpeg xfade 'wipebl' (wipe). | {'duration': 0.6} |
| `transition.wipebr` | Wipebr | builtin | wipe | FFmpeg xfade 'wipebr' (wipe). | {'duration': 0.6} |
| `transition.wipedown` | Wipedown | builtin | wipe | FFmpeg xfade 'wipedown' (wipe). | {'duration': 0.6} |
| `transition.wipeleft` | Wipeleft | builtin | wipe | FFmpeg xfade 'wipeleft' (wipe). | {'duration': 0.6} |
| `transition.wiperight` | Wiperight | builtin | wipe | FFmpeg xfade 'wiperight' (wipe). | {'duration': 0.6} |
| `transition.wipetl` | Wipetl | builtin | wipe | FFmpeg xfade 'wipetl' (wipe). | {'duration': 0.6} |
| `transition.wipetr` | Wipetr | builtin | wipe | FFmpeg xfade 'wipetr' (wipe). | {'duration': 0.6} |
| `transition.wipeup` | Wipeup | builtin | wipe | FFmpeg xfade 'wipeup' (wipe). | {'duration': 0.6} |
| `transition.zoom_blur` | Zoom blur | builtin | zoom | Zooms through the frame with radial blur. Custom expression (FFmpeg >= 6); falls back to zoomin on older FFmpeg. | {'duration': 0.6} |
| `transition.zoomin` | Zoomin | builtin | zoom | FFmpeg xfade 'zoomin' (zoom). | {'duration': 0.6} |

