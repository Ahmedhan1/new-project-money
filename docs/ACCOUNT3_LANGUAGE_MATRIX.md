# Account 3: language matrix, translation QA, dialects, dubbing manifest and subtitles (agent D)

Branch `work/account-3-d-language`. Code: `nexus/intelligence/language/` (machine-readable matrix:
`nexus/intelligence/language/matrix.yaml`). Tests: `tests/fast/test_intel_language.py` (33 tests, no models, no network).
Demo: `scripts/account3/language_demo.py` -> `docs/results/account3/language_demo.json`.

This package builds on `nexus/localize.py` (MADLAD-400 MT, sentence groups), `nexus/speech/packs.py` +
`nexus/registry/languages.yaml` (packs, dialect lexicons, segmentation, bidi) and `nexus/speech/tts.py` (Piper). It does
not change them. The changes it needs are listed under **Integration requests**.

Status words follow AGENTS.md: *implemented* = code exists, *tested* = fast test passes, *verified* = ran on real
models with evidence. Nothing here was used by a native-speaker reviewer yet.

## 1. Language matrix (15 languages)

Licence facts were read on **2026-11-12** from the first-party page named in `matrix.yaml` (`source` on every row).
`commercial_use` is `"yes"` only when the weights, the training data and the base model all allow it. Unknown =
`"unverified"`, which the voice selector treats like `"no"` for commercial output.

| Tag | ASR | MT options (commercial) | Commercially cleared voice (installed?) | Other voices (state) | Subtitles: chars/line, cps (source) |
|---|---|---|---|---|---|
| en | Whisper small (MIT) | - | en_US-joe, en_US-ljspeech (installed) | Kokoro (Apache-2.0, not installed) | 42, 17 (default, unverified) |
| de | Whisper | MADLAD-400, router LLM | de_DE-thorsten (installed), de_DE-kerstin (attribution Hi-Fi TTS) | - | 42, 17 (Netflix DE, search excerpt) |
| fr | Whisper | MADLAD, router | fr_FR-siwis, fr_FR-mls (CC BY 4.0, attribution) | upmc, tom: unverified | 42, 17 (default) |
| ar (MSA) | Whisper (`ar`) | MADLAD, router | **none** | ar_JO-kareem: unverified; MMS-TTS: no (CC-BY-NC) | 42, 20 (Netflix AR) |
| ar-EG | Whisper (`ar`, no dialect code) | MADLAD `<2arz>`, router + dialect pass | **none** | kareem (unverified, Jordanian-sounding) | 42, 20 (Netflix AR) |
| ar-Gulf | Whisper (`ar`) | MADLAD `<2ar>` (no Gulf target: afb/acm/ars absent) + dialect pass | **none** | kareem (unverified) | 42, 20 (Netflix AR) |
| zh | Whisper | MADLAD, router | zh_CN-chaowen (needs Piper >= 1.5 + g2pW; attribution Hi-Fi TTS) | huayan: unverified; xiao_ya: no | 16, 9 (Netflix zh-Hans) |
| ru | Whisper | MADLAD, router | ru_RU-denis | dmitri, irina: unverified; ruslan: no | 42, 17 (Netflix RU) |
| hi | Whisper | MADLAD, router | Kokoro Hindi voices (Apache-2.0, **not installed**) | rohan: unverified (IndicTTS licence PDF unread); pratham, priyamvada: no | 42, 22 (Netflix HI) |
| es | Whisper | MADLAD, router | es_ES-davefx, es_ES-sharvard (attribution) | - | 42, 17 (default) |
| pt | Whisper | MADLAD (one `pt` tag, BR/PT not controlled), router | pt_BR-cadu | faber: unverified | 42, 17 (default) |
| tr | Whisper | MADLAD, router | **none** | tr_TR-dfki: no (CC BY-NC-SA) | 42, 17 (default) |
| ja | Whisper | MADLAD, router | Kokoro Japanese (not installed) | hi_fi_captain: no | 13 full-width, 4 (Netflix JA) |
| ko | Whisper | MADLAD `<2ko>` (in vocab), router; **no language pack yet** | **none** (OpenVoice V2 cloning only with consent) | ko_KR-kss: no (CC BY-NC-SA) | 16 (Latin/space = 0.5), 12 (Netflix KO) |
| it | Whisper | MADLAD `<2it>` (in vocab), router; **no language pack yet** | Kokoro Italian (not installed) | paola: unverified (lessac base); serena: unverified (synthetic Qwen3-TTS data, provenance not verified by its author); riccardo: unverified (M-AILABS page unreachable) | 42, 17 (Netflix IT) |

Engines checked on first-party pages (2026-11-12):

| Engine | Licence | Commercial | Evidence |
|---|---|---|---|
| MADLAD-400 3B | Apache-2.0 | yes (card: research model, not assessed for production) | huggingface.co/google/madlad400-3b-mt |
| **NLLB-200** | **CC-BY-NC-4.0** | **no** | huggingface.co/facebook/nllb-200-distilled-600M: metadata `cc-by-nc-4.0`; "a research model and is not released for production deployment" |
| Router LLM (local Qwen3-4B) | Apache-2.0 | yes | capability_registry/providers.yaml |
| Kokoro-82M | Apache-2.0 | yes | huggingface.co/hexgrad/Kokoro-82M; VOICES.md: en-US/en-GB/ja/zh/es/fr/hi/it/pt-BR, **no ar/de/ru/ko/tr**. Card: training data includes synthetic audio from closed TTS providers (recorded as a provenance note) |
| **MMS-TTS** | **CC-BY-NC 4.0** | **no** | huggingface.co/facebook/mms-tts-ara: "The model is licensed as CC-BY-NC 4.0." |
| **XTTS-v2** | **Coqui Public Model License** | **no** | huggingface.co/coqui/XTTS-v2; coqui.ai/cpml returned 404 on 2026-11-12, so the CPML text was not re-read; treated as non-commercial |
| OpenVoice V2 | MIT | yes | github.com/myshell-ai/OpenVoice: "V1 and V2 are MIT Licensed. Free for both commercial and research use." Base TTS (MeloTTS) licence not read |
| Whisper / faster-whisper small | MIT | yes | from languages.yaml sources |

Subtitle limits: Netflix Timed Text Style Guides on partnerhelp.netflixstudios.com (Arabic 215517947, Simplified
Chinese 215986007, Japanese 215767517, Korean 216001127, Hindi 115003196707, Russian 215346638, Italian 215349898;
General Requirements 215758617: 2 lines, 5/6 s minimum, 7 s maximum). German (217351587) from a search excerpt only.
fr/es/pt/tr/en use the 42 x 2 / 17 cps editorial default and are marked unverified. Fonts per script come from the
pack font sets (Noto Naskh Arabic, Noto Sans CJK SC/JP, Noto Sans Devanagari); Korean has no font set yet (IR-1).

## 2. Translation QA (`translation_qa.py`) - implemented, tested, run on real MT

Per line: sentence alignment (missing / extra sentence; does not split German ordinals "Ramses II. gebaut"), length
band per script, numbers (Arabic-Indic and Devanagari digits, 1,200 / 1.200 / 1 200, 3,5 = 3.5; spelled-out numbers
are reported, not recognised), glossary terms per target language, protected names (verbatim in Latin targets,
confirmed through the back-translation in other scripts, otherwise reported as unverifiable), repetition (n-grams and
doubled words), duplicate lines, **unsupported content** (back-translation words with no source counterpart) and
**omission back-check** (source content words that do not come back). Back-translation: model router task
`translation`, privacy chosen by the caller; output that is not English or copies the input is retried line by line
and never scored (the first German run showed Qwen3-4B copying German back as "English").

Confidence per line = 1 minus 0.5/0.3/0.1 per critical/major/minor finding, minus 0.15 when meaning was not
cross-checked, capped by 0.4 + 0.6 x back-translation coverage. A heuristic for ranking review work, **not a calibrated
probability**.

Approval workflow: `build_manifest` writes a `LocalizationManifest` (contracts v1) with per-line machine text,
confidence, findings and status; `review_line` records approve / correct / reject by a named reviewer (machine text
kept); `approval_state` = machine_draft / in_review / approved / rejected; `commercially_ready(lang)` is true only when a
human approved every line.

Deliberately broken translations in the tests (dropped sentence, changed number, wrong glossary term, dropped name,
invented sentence, repetition loop, half line missing, invented back-translation content, line-count mismatch,
duplicate line) are all caught.

## 3. Egyptian vs Gulf Arabic (`dialect.py`) - implemented, tested, run on real MT

Two profiles plus MSA, each a weighted marker lexicon (1.0 = distinctive, 0.3-0.5 = shared, e.g. هذا is Gulf **and**
MSA, اللي is EG **and** Gulf). Examples: EG ده/دي/مش/عشان/إزاي/كده/دلوقتي/فين/مفيش; Gulf هذي/وايد/شلون/ليش/الحين/زين/شنو/مو;
MSA-only سوف/لماذا/ليس/لقد/الذي/الآن/هذه. Forbidden markers: the other dialect's distinctive words always, MSA-only words
in the `colloquial` register (allowed in `semi_formal`).

Classifier: EG / Gulf / MSA scores (shares summing to 1), label, evidence words; `uncertain` with too little evidence
or a close call. Adaptation: one router call (task `translation`, caller's privacy) with the pack's style text and the
forbidden list; each line is accepted only if it stays Arabic, keeps every number, keeps length within 0.6-1.6x and is
at least as dialectal as the lexical fallback; otherwise the deterministic lexical pass (pack lexicon + extra phrases
such as لا يوجد -> مفيش / ما فيه) is used. Each line records which method produced it.

Limits: marker lexicons cannot judge naturalness, verb morphology or register; the router model is a 4B local model.
A native Egyptian / Gulf reviewer must approve every line.

## 4. Dubbing manifest (`dubbing.py`) - implemented, tested

Per line: text, voice, licence, attribution, consent id, timing target {start, end, slot}, estimated duration
(character-rate heuristic, labelled as such), speed needed, max speed 1.25, minimum picture speed 0.85, action
(ok / brisker_read / shorten_line). Voice selection uses the matrix: commercial output gets only `"yes"` voices; with no
such voice the manifest is still produced with `voice: null` and `blocked: <reason>` (subtitles only). Voice cloning
needs an engine cleared for commercial use (OpenVoice V2; XTTS-v2 is refused) and a consent record: the signed
document's sha256, languages, uses, dates; revoked, expired, wrong-language, wrong-use or changed documents are refused.
Rendering is Account 2's; `render_demo_line` calls `nexus.speech.tts.synthesize` for one demo line and refuses if Piper
falls back to another voice.

## 5. Subtitles (`subtitles.py`) - implemented, tested

`build_cues` (pack segmentation where a pack exists, then wrapped to the matrix limits; Korean width counts Latin/space
as 0.5; CJK keeps numbers/Latin words whole and never starts a line with closing punctuation), `qa` (cps, line length,
lines per cue, 5/6 s - 7 s, overlap), exporters `to_srt`, `to_vtt` (escaped), `to_ass` (styled: pack font family,
charset 178 Arabic / 134 GB2312 / 128 Shift-JIS / 129 Hangul, outline, safe margins; braces and backslashes replaced so
text cannot inject ASS overrides). RTL: every line starts with RLM and Latin runs are wrapped in LRI...PDI.
Products: **full** (verbatim) and **condensed** (one line, filler words removed, <= 75 % of the max cps; cues that still
read too fast are listed in `condense_todo` for a human or the router - nothing is cut silently).

## 6. Demo results (verified on this machine, 2026-11-12)

`scripts/account3/language_demo.py` translated a fixed 8-sentence English text about the Aswan High Dam (numbers,
names, dates) into de, fr, ar-EG, ar-Gulf, zh, ru, hi, es with the existing `localize.translate_sentences` (MADLAD-400 +
local-LLM fallback + pack rules), then ran the dialect pass (ar-EG, ar-Gulf), back-translation through the model router
(local Qwen3-4B, privacy `internal`), the QA, SRT/VTT/ASS export (full + condensed) and a dubbing manifest per
language. Everything is in `docs/results/account3/language_demo.json` (per line: MT output, final text, back-translation,
every finding, confidence; SRT text; ASS style; dubbing lines; voice-over results). `--reqa` recomputes the
deterministic QA from the stored outputs without models.

| Lang | Lines flagged (of 8) | Mean confidence | Voice-over line 1 | Subtitle QA (full) |
|---|---|---|---|---|
| de | 1, 2, 6 | 0.74 | de_DE-thorsten (CC0), 7.6 s | 2 findings (reading speed) |
| fr | 1 | 0.81 | fr_FR-siwis (CC BY 4.0, attribution SIWIS + Hi-Fi TTS), 5.5 s | 6 findings (reading speed) |
| ar-EG | 5, 6 | 0.86 | none: no commercially cleared voice (kareem unverified) | passed |
| ar-Gulf | 1, 2, 4, 6, 7 | 0.61 | none: no commercially cleared voice | passed |
| zh | 7, 8 | 0.87 | **failed**: zh_CN-chaowen raised "Error: # channels not specified" in Piper (IR-9) | passed |
| ru | 6, 7 | 0.88 | ru_RU-denis (CC0), 8.0 s | 2 findings |
| hi | 3, 6, 8 | 0.84 | none rendered: the only cleared option (Kokoro) is not installed | passed |
| es | 1, 2, 5 | 0.77 | es_ES-davefx (CC0), 7.0 s | 4 findings |

Real errors the QA caught (agent's reading; no native reviewer):
- **de 1**: MADLAD wrote "Aswan-**Hochbehinderung**" ("high disability") for "High Dam". Caught by glossary + name
  check. The back-translator *repaired* it to "High Dam", so back-translation alone would have missed it.
- **de 2, de 6**: sentences cut in half ("Er ist 111 Meter hoch, ca." / "bedrohte die für Ramses II."): `mt._sentences`
  treats "ca." and "II." as sentence ends and `mt.translate` keeps only as many sentences as the source had (IR-7).
  Caught as too_short + number_missing (3830) and omission.
- **ar-Gulf 1**: MSA MT invented "وتم الانتهاء من بنائه في عام ٢٠٠٥" ("completed in 2005"). Caught (number_added 2005).
- **ar-Gulf 2, 4**: 3,830 and 2,100 came out as "٨٣٠ ٣" and "١٠٠ ٢" (digit groups reversed). Caught as number
  missing/added. Root cause (MADLAD output vs. typography) not isolated (IR-8).
- **es 2**: invented "y 111 de ancho" (111 m wide). **es 5**: invented "que fue inaugurada en 1960". Both caught
  (number_added; added_clause).
- **ar-EG 5**: MT added "وساعدوا على إنجازه" ("and they helped complete it"). Caught by the added-clause check that was
  written after the first run missed it.
- **fr 1**: "L'ouvrage de l'Aswan" dropped "High Dam", "Nile" and "Egypt". Caught (glossary, name, omission).
- ru 6: "Абу Симбели", "Рамесеса" vs the glossary Абу-Симбел / Рамсес (caught, glossary).

Findings that are probably false or debatable: zh 7 uses the short form 教科文组织 instead of the glossary's full
联合国教科文组织; hi 3/6 use spelling variants (नासर, सिम्बेल, रामेसेस) of the glossary terms (the glossary was written by
the agent, not by a linguist); "unsupported_content" on zh 8 / hi 8 / ru 7 is back-translation paraphrase. Glossary
and name findings are only as good as the glossary.

Dialects. The router (local Qwen3-4B) is **not good enough** for Egyptian/Gulf rewriting: run 1 accepted invented
non-words (بيو، كايل، "واي بيرف") and a "but" -> "why" swap (ولكنه -> وليش). The acceptance check now rejects router lines
with words that have no source or lexicon counterpart, or that add question words; in the final run ar-EG used 4 router
lines and 4 lexical, ar-Gulf 8/8 lexical (`first_run_arabic` in the JSON keeps run 1 for reference). The MADLAD `<2arz>`
output is itself partly Egyptian (اللي بتقدر تحمل ... من المية), so ar-EG and ar-Gulf texts differ clearly; the classifier
labels the ar-EG text **EG** (0.63/0.19/0.18) and the ar-Gulf text **MSA** (0.14/0.14/0.71), which is the honest
result: K-15 (Gulf reads as MSA) is still open; the lexical fallback only changes function words that the MT happens to
use. `dialect_contrast`: the same 8 MSA MT lines through the EG and Gulf lexical passes - whole text classified MSA ->
EG -> Gulf as expected, but only 3 of 8 lines change. Per line, most lines are `uncertain` (too little evidence),
which the classifier reports instead of guessing. Native-quality Gulf needs a better model (owner-authorised hosted
provider via the router, privacy `public` only for public scripts) or a human.

Subtitles: the full SRT of every language is in the JSON; ASS styles use Noto Naskh Arabic (charset 178) and WenQuanYi
Zen Hei (134) on this machine. Reading-speed findings for de/fr/ru/es are real: the timing comes from the English
narration, and the translations are longer - the condensed product lists 8-14 cues per language to shorten
(`condense_todo`); nothing was cut automatically.

Performance (shared 4-CPU machine): MT 16-183 s per language for 8 sentences (includes model load and LLM retries),
back-translation 30-60 s, dialect pass about 1-3 min.

## Integration requests (for the lead)

**IR-1 - add Korean and Italian packs** (`nexus/registry/languages.yaml`, under `packs:`; plus a Korean font set under
`font_sets:`). MADLAD `<2ko>` and `<2it>` were checked in the installed vocabulary; add `ko` and `it` to
`mt_supported_targets`. No voice is listed as commercial (none is cleared):

```yaml
  cjk_kr:
    files:
      - "{USERFONTS}/NotoSansKR-Bold.otf"
      - "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc#1"
      - "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc#1"
      - /usr/share/fonts/truetype/nanum/NanumGothicBold.ttf
      - "{WINFONTS}/malgunbd.ttf"
    families: [Noto Sans CJK KR, NanumGothic, Malgun Gothic]
```
```yaml
  ko:
    name: Korean
    native_name: 한국어
    script: Hang
    direction: ltr
    status: implemented
    sample: "태양광 패널은 햇빛을 직접 전기로 바꿉니다."
    fonts: [cjk_kr, last_resort]
    asr: {model: whisper-small, code: ko, licence: MIT, commercial_use: "yes"}
    mt: {model: mt-madlad400-3b, target: ko, licence: "Apache-2.0", commercial_use: "yes", length_ratio: [0.25, 1.0]}
    tts:
      voices:
        - id: ko_KR-kss-medium
          engine: piper
          path: ko/ko_KR/kss/medium
          source_url: https://huggingface.co/rhasspy/piper-voices/blob/main/ko/ko_KR/kss/medium/MODEL_CARD
          licence: "KSS dataset CC BY-NC-SA 4.0"
          dataset_licence: CC BY-NC-SA 4.0
          commercial_use: "no"
          card_read_on: "2026-11-12"
      fallback: {engine: espeak-ng, voice: ko, licence: "GPL-3.0 engine; generated audio is not covered by the GPL", commercial_use: "yes"}
    subtitles: {max_chars_per_line: 16, max_cps: 12}
    typography: {quotes: ["“", "”"]}
    needs_native_review: false

  it:
    name: Italian
    native_name: Italiano
    script: Latn
    direction: ltr
    status: implemented
    sample: "I pannelli solari trasformano la luce del sole in elettricità."
    fonts: [latin, last_resort]
    asr: {model: whisper-small, code: it, licence: MIT, commercial_use: "yes"}
    mt: {model: mt-madlad400-3b, target: it, licence: "Apache-2.0", commercial_use: "yes", length_ratio: [0.6, 1.5]}
    tts:
      voices:
        - id: it_IT-paola-medium
          engine: piper
          path: it/it_IT/paola/medium
          source_url: https://huggingface.co/rhasspy/piper-voices/blob/main/it/it_IT/paola/medium/MODEL_CARD
          licence: "dataset CC0 (paolapersico1/Voice-Dataset-Italian); fine-tuned from lessac (Blizzard 2013 research licence)"
          dataset_licence: CC0
          commercial_use: "unverified"
          card_read_on: "2026-11-12"
      fallback: {engine: espeak-ng, voice: it, licence: "GPL-3.0 engine; generated audio is not covered by the GPL", commercial_use: "yes"}
    subtitles: {max_cps: 17}
    typography: {quotes: ["«", "»"]}
    needs_native_review: false
```
`Hang` must be added to `SCRIPTS` and `OWN` in `nexus/speech/packs.py` (`"Hang": {"Hang", "Hani"}`) and to
`char_script` (Hangul syllables U+AC00-U+D7A3, Jamo U+1100-U+11FF, U+3130-U+318F).

**IR-2 - Arabic word boundary bug in `packs.adapt`** (`nexus/speech/packs.py` line 349). The boundary class
`[\u0600-\u06FF]` contains Arabic punctuation (، U+060C, ؛ U+061B, ؟ U+061F), so a word directly before them is never
adapted: "...رخيصة الآن؟" kept MSA الآن in both dialects (reproduced in `test_lexical_fallback_gives_different_dialects`;
`dialect.lexical_adapt` works around it). Patch:
```python
_AR_WORD = r"(?<![\u0621-\u064A\u066E-\u06D3]){w}(?![\u0621-\u064A\u066E-\u06D3\u064B-\u0652])"
```
Same class problem in `dialect_density`, which strips only `،.؟!,?` - fine - but `localize.language_review`'s Egyptian
marker check should call `nexus.intelligence.language.dialect.classify` instead (IR-4).

**IR-3 - use the line-level QA in `localize.language_review`** (`nexus/localize.py`): after the existing checks add
```python
    from .intelligence.language import translation_qa as tq
    detail = tq.check_lines(source, target, lang, glossary=None, back=back)
    for i, ln in enumerate(detail["lines"], 1):
        for f in ln["findings"]:
            if f["severity"] in ("critical", "major"):
                checks.append(qa._c(f"{f['check']} [{lang}] line {i}", False, f["detail"], "major", fix="retranslate"))
    review_lines_confidence = [ln["confidence"] for ln in detail["lines"]]
```
and return `review_lines_confidence` in the review dict. This closes K-14 detection gaps (sentence-level omissions,
added sentences, repetition, name loss) that the length-only check misses.

**IR-4 - dialect pass in `localize.localize`** (`nexus/localize.py`, after `lines = translate_sentences(...)`):
```python
        if lang in ("ar-EG", "ar-Gulf"):
            from .intelligence.language import dialect as _d
            privacy = "private"  # user media / unreleased scripts stay local
            lines = [a["text"] for a in _d.adapt_lines(lines, lang, privacy=privacy)]
```
and replace the `if lang == "ar-EG": ... EGYPTIAN_MARKERS` block in `language_review` with a check on
`_d.classify(joined)["label"] == {"ar-EG": "EG", "ar-Gulf": "Gulf"}[lang]` (addresses K-15: Gulf output reading as MSA).

**IR-5 - subtitle limits from the Netflix guides** (`languages.yaml`): ar / ar-EG / ar-Gulf `max_cps: 20`, hi
`max_cps: 22`, ja `max_cps: 4` (Japanese guide says 4, pack has 5). Only where the lead wants Netflix limits as the
product default.

**IR-6 - docs/FEATURE_MATRIX.md "Languages" rows**: add "translation QA per line (omission / addition / names /
glossary / repetition / back-translation) - T (tested), V on the demo" and "Egyptian vs Gulf classifier + router
adaptation - T, demo evidence in docs/results/account3/language_demo.json".

**IR-7 - MT truncates sentences at abbreviations / ordinals** (`nexus/speech/mt.py`, `_sentences`). German "ca. 3.830"
and "Ramses II. gebaut" were cut because `translate()` keeps `len(_sentences(src))` sentences of the output. Patch:
```python
def _sentences(text: str) -> list[str]:
    parts = [s for s in re.split(r"(?<=[!?؟。…])\s+|(?<=(?<!\d)\.)\s+", text.strip()) if s]
    out: list[str] = []
    for s in parts:
        # "ca. 3", "II. gebaut", "z. B." are not sentence ends: the next piece starts lower-case or with a digit,
        # or the previous piece ends in an abbreviation / Roman numeral
        if out and (s[:1].islower() or s[:1].isdigit() or re.search(r"(?:\b(?:ca|bzw|vgl|Nr|usw|etc|env|approx|z\.\s?B|d\.\s?h)|\b[IVXLC]+)\.$", out[-1])):
            out[-1] += " " + s
        else:
            out.append(s)
    return out
```
(`translation_qa.sentences` already merges lower-case continuations; test `test_sentence_split_keeps_german_ordinals`.)

**IR-8 - reversed digit groups in Arabic output** ("3,830" -> "٨٣٠ ٣", "2,100" -> "١٠٠ ٢" in the MSA run). Suggested
fix in `localize._finish_line` before typography: if the source has a grouped number and the target has
`(\d{3}) (\d{1,3})` whose swap equals it, swap back and record the repair; otherwise QA keeps flagging it. Needs
the raw MADLAD output to locate the cause (not isolated here).

**IR-9 - zh_CN-chaowen voice fails in Piper** ("Error: # channels not specified"; the card says it needs Piper >= 1.5
with g2pW). Until fixed, `languages.yaml` zh should not offer chaowen first, or Account 2 should catch it and fall back
to subtitles.

## Unverified / blocked

- Netflix German limits from a search excerpt; fr/es/pt/tr/en limits are editorial defaults.
- XTTS-v2 CPML text (404), M-AILABS licence (host unreachable), IndicTTS licence PDF (timed out): kept unverified/no.
- Kokoro and OpenVoice are not installed; no Kokoro/OpenVoice output was produced or listened to.
- No native-speaker review of any line; glossary spellings were chosen by the agent, not a linguist.


## Readiness per language (`language/readiness.py`, `GET /api/a3/language/readiness`)
Eight dimensions with weights {'transcription': 10, 'translation': 15, 'subtitle_rendering': 15, 'human_reviewed': 15, 'tts': 10, 'commercial_dubbing_licensed': 10, 'lip_sync': 5, 'end_to_end_validated': 20}; states verified (measured here and passed) / available (licensed, not
validated end to end) / partial / unavailable / n/a. No language is "publishable" without native-speaker review
(>= 20 reviewed lines at >= 90 % acceptance). Run of 2026-11-12T22:49:58 (`docs/results/account3/language_readiness.json`):

| Lang | Score | Level | transcription | translation | subtitle_rendering | human_reviewed | tts | commercial_dubbing_licensed | lip_sync | end_to_end_validated |
|---|---|---|---|---|---|---|---|---|---|---|
| en | 87.0 | subtitles_publishable | available | n/a | verified | n/a | available | verified | unavailable | verified |
| de | 56.0 | draft_needs_native_review | available | available | verified | unavailable | verified | verified | unavailable | partial |
| fr | 56.0 | draft_needs_native_review | available | available | verified | unavailable | verified | verified | unavailable | partial |
| ar | 33.0 | draft_needs_native_review | available | available | verified | unavailable | partial | unavailable | unavailable | unavailable |
| ar-EG | 36.0 | draft_needs_native_review | partial | available | verified | unavailable | partial | unavailable | unavailable | partial |
| ar-Gulf | 31.5 | draft_needs_native_review | partial | partial | verified | unavailable | partial | unavailable | unavailable | partial |
| zh | 49.0 | draft_needs_native_review | available | available | verified | unavailable | partial | verified | unavailable | partial |
| ru | 56.0 | draft_needs_native_review | available | available | verified | unavailable | verified | verified | unavailable | partial |
| hi | 49.0 | draft_needs_native_review | available | available | verified | unavailable | partial | verified | unavailable | partial |
| es | 56.0 | draft_needs_native_review | available | available | verified | unavailable | verified | verified | unavailable | partial |
| pt | 43.0 | draft_needs_native_review | available | available | verified | unavailable | partial | verified | unavailable | unavailable |
| tr | 33.0 | draft_needs_native_review | available | available | verified | unavailable | partial | unavailable | unavailable | unavailable |
| ja | 37.0 | draft_needs_native_review | available | available | available | unavailable | partial | verified | unavailable | unavailable |
| ko | 27.0 | draft_needs_native_review | available | available | available | unavailable | partial | unavailable | unavailable | unavailable |
| it | 43.0 | draft_needs_native_review | available | available | verified | unavailable | partial | verified | unavailable | unavailable |

Notes: subtitle rendering is *verified* only by an OCR read-back of the rendered line (ja/ko have no Tesseract pack here,
so they stay *available*); zh TTS is *partial* because the cleared voice failed to render in the real run; ar-Gulf
translation is *partial* (output still classifies as MSA, K-15); lip-sync has no cleared model anywhere.

### Gulf Arabic
Still not distinct in generation. What changed: the readiness score, the dubbing gate and the Demo 10 evidence all
report it as a blocker; the Kaggle benchmark adds Gulf/Egyptian items so the router can pick the model that writes it
best (see ACCOUNT3_MODEL_BENCHMARKS.md); and the native-review workflow is the acceptance path.

### Terminology memory and native review
`language/terminology.py`: approved renderings per language with origin (native_review > owner > glossary > machine);
a machine suggestion can never overwrite a reviewer's term; `/language/check` applies the memory automatically.
`language/review.py` + `/api/a3/language/review`: queue lines with their QA findings, record approve / correct / reject
with a reviewer label, corrected terms flow into the memory, stats feed the readiness score. Nothing leaves the machine.

### Per-output language gate (publishing, 2026-11-13)
`publishing/release_gate.language()` evaluates every localized output before publishing. It records:
- language and dialect (`ar` + `EG` gives `ar-EG`, and `ar` + `Gulf` gives `ar-Gulf`; other dialects are refused);
- the translation QA, terminology and OCR/glyph results;
- voice licence and consent;
- whether native review is required;
- the reviewer approval state: not_submitted, pending, rejected, invalid or approved.

Native review is required for every non-source language that has not met the readiness bar (≥ 20 reviewed lines at
≥ 90 % acceptance). It is **always** required for ar-EG, ar-Gulf, hi and zh. Approval comes only from
`language/review.record()` for **the same variety**: an approved ar-Gulf line never clears an ar-EG output.

Without a reviewer the output stays a draft: a private upload is allowed and public publishing is blocked. A voice
without a cleared licence, or with consent problems, blocks every upload. Tests: `tests/fast/test_intel_release_gate.py`
(language section). No native reviewer has reviewed any line yet, so every language other than en is
`draft_needs_native_review` today.
