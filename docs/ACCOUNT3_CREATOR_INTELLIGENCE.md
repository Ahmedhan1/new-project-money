# Account 3 / agent A - Creator Reference Intelligence

Package: `nexus/intelligence/creators/` - turns lawfully accessed reference videos into **structural** profiles
(`CreatorReferenceProfile`), abstract styles (`CreativeStyleProfile`) and comparisons. Patterns, never copies.

Status: **implemented + tested** (fast tests `tests/fast/test_intel_creators.py`); demo evidence in
`docs/results/account3/creators_profiles.json` and `creators_compare.json` (see "Demo evidence"). Not yet
**verified** in the integrated product (no API/UI wiring - see Integration requests).

## API

```python
from nexus.intelligence.creators import analyze_reference, compare, style_from, to_account2_reference_style
p = analyze_reference("ref.mp4", rights="public-domain", meta={"creator": "NASA", "format": "science_documentary",
                      "licence_url": "..."}, use_router=False)   # local file: measured locally, nothing uploaded
m = analyze_reference("https://youtu.be/<id>", rights="", meta={"title": "...", "views": 123})  # metadata only, no download
report = compare([p1, p2, p3]);  style = style_from([p1, p2, p3], name="...")
a2 = to_account2_reference_style(style, rights="public-domain")  # Account 2 ReferenceStyleProfile (name/source/metrics/structure/rights)
```

| module | role |
|---|---|
| `measure.py` | objective measurements: probe, FFmpeg scene cuts (`media.analyze.detect_scenes_ffmpeg`), silences, integrated + windowed loudness (first 5/15 s vs rest), visual quality (`visual_quality_ffmpeg`), optional Whisper word timings (`speech.asr`, under `jobs.heavy_lock`, unloaded afterwards) or owner-supplied captions. Pure core `from_parts()` is testable without media. |
| `profile.py` | `analyze_reference()`; rights gate; rule layer (`RULES`, NEXUS-internal thresholds, not industry norms); optional router call `reference_interpretation`, privacy `private`; NoProviderAvailable -> recorded as `not_available`, rules only. |
| `metadata.py` | YouTube Data API v3 `videos.list` only (key `NEXUS_KEY_YOUTUBE`); hidden counts stay absent; every packaging field records `source` (`youtube_data_api_v3` / `owner_entered`). |
| `compare.py` | `compare()`, `style_from()`, Account 2 projection. |
| `library.py` + `patterns.yaml` | curated archetypes; each claim has `src` (declared source) or `hypothesis`. |

### What is measured vs interpreted
Every profile has a `provenance` block. **Measured**: duration, shots, cuts/min, mean/median/longest shot, pacing curve
(cuts/min per 10 % of runtime), opening windows 3/5/15 s (cuts, speech onset, words, words/s, question words, numbers,
stakes words, loudness), loudness jump (first 5 s vs rest), speech share, words per speaking minute, silence share,
upper bound of non-speech audio share, resolution/fps, sharpness/brightness. **Not measured** (said so in the profile):
on-screen text (no OCR model deployed), music vs effects (no music classifier; non-speech share is an upper bound).
**Interpreted** (each `{value, confidence, based_on, rule}`): hook type, pacing shape, edit tempo, audio mode,
engagement mechanisms, strengths, weaknesses, techniques; `story.structure` only when a local model answered.

### compare()
Shared patterns / differences (categorical readings and metrics with <= 25 % spread), strongest opening by seven explicit
criteria (min-max normalised, 1 point each: cuts in first 3 s, words/s in first 5 s, earlier speech onset, question
words, numbers, stakes words, loudness jump), adaptable techniques, what NEXUS should do differently (stated as
hypotheses to test), an original-angle suggestion (an archetype none of the references represent + the strongest
measured hook). Every statement carries `cites: ["<profile id>:<field path>"]`. The report's `disclaimer`: patterns are
not proof of causation; the opening ranking says nothing about audience response.

### style_from()
Aggregated pacing (median/min/max), hook patterns, structure (pacing shapes + matching library archetype with sources),
visual/audio rules and `do_not_copy` (titles, channel/creator branding, opening lines when kept, catchphrases, logos,
music, characters, sets, footage).

## Pattern library (`patterns.yaml`)
Archetypes: challenge/high-stakes, Arabic educational explainer (El Daheeh-style), science documentary, travel vlog,
interview/podcast, investigative, product review, premium ad, short-form story. **No pacing numbers**: no public
first-party source states one, so all `cuts_per_min` are null and are measured from references instead. Claims without a
public source are marked `src: hypothesis` (most of interview, investigative, product review, premium ad).

Sources (retrieved 2026-11-12):
- MrBeast production onboarding document (2024; published by Rosanna Pansino; authenticity *reported*, not verified by us):
  transcription https://www.alexanderjarvis.com/memo-how-to-succeed-in-mrbeast-production/ ; coverage
  https://simonwillison.net/2024/Sep/15/how-to-succeed-in-mrbeast-production/ ,
  https://www.netinfluencer.com/mrbeast-productions-leaked-internal-document-rosanna-pansino/
- Kurzgesagt, "Our video process" (first party): https://kurzgesagt.org/youtube/
- AUC Today, "Behind the Nerd" (secondary, on the El Da7ee7 writing team/book): https://auctoday.aucegypt.edu/news/behind-nerd .
  The BBC interview with Ahmed El Ghandour was found only via secondary pages; not used. The El Daheeh hook/graphics habits
  are therefore marked `hypothesis`.
- No Film School on Casey Neistat (secondary): https://nofilmschool.com/2016/08/7-storytelling-techniques-you-can-learn-filmmaker-youtube-star-casey-neistat
- YouTube Help, key moments for audience retention ("Intro" = share still watching after the first 30 s) (first party):
  https://support.google.com/youtube/answer/9314415
- Searched for first-party YouTube Shorts hook guidance: only third-party blogs found ("1-3 s hook" claims) - **unverified, not used**.

## Rules kept
Local files need an owner-stated rights basis (`owned, licensed, public-domain, cc0, cc-by, cc-by-sa, commentary`).
URLs are never downloaded. Transcripts are reduced to counts; the opening phrase (<= 12 words) is kept only with
`keep_quote=True` and then lands in `do_not_copy`. Router calls are `privacy="private"` and wrapped in `jobs.heavy_lock()`.

## Demo evidence
See section appended by the demo run below.

## Integration requests (for the lead)
1. `nexus/registry/components.yaml`: declare a zero-cost component for the YouTube Data API (`network_hosts: [www.googleapis.com]`,
   free quota, key without billing) - `costguard.check_host` currently blocks it, so `metadata.youtube_metadata` returns
   `status: blocked` even with a key. Also `archive.org` if archive.org references are wanted (NASA Images and Wikimedia
   Commons are already declared).
2. API/UI: `POST /intel/references` (file upload or URL + rights + meta) -> `analyze_reference`; `POST /intel/references/compare`.
   Store profiles under `NEXUS_HOME/intel/references/` (private).
3. Account 2: accept `to_account2_reference_style()` output as `ReferenceStyleProfile` input to the director.
4. `contracts.make(name, **fields)` cannot build `CreativeStyleProfile` (its `name` field collides with the positional
   parameter); `style_from` builds the dict and calls `check()`. Suggest renaming the parameter to `_contract`.
5. Optional OCR (on-screen text) and a music/speech classifier would turn two `not_measured` fields into measurements.
