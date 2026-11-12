"""LANG-1: language packs, dialect profiles, script-correct captions, licence-gated voices, language QA.
MT/TTS are stubbed; captions are really rendered with FFmpeg/libass and Pillow. No models, no network."""
import subprocess

import numpy as np
import pytest

from nexus import localize
from nexus.speech import packs
from nexus.timeline import text as ttext

PRIORITY = ["en", "de", "fr", "ar", "ar-EG", "ar-Gulf", "zh", "ru", "hi"]
SAMPLES = {"ar": "الطاقة الشمسية مصدر نظيف للكهرباء", "zh": "太阳能是一种清洁的电力来源", "hi": "सौर ऊर्जा बिजली का स्वच्छ स्रोत है",
           "ru": "Солнечная энергия — чистый источник электричества"}


def test_priority_languages_and_extras_load_and_validate():
    assert set(PRIORITY) <= set(packs.tags()) and len(packs.tags()) >= 12
    assert packs.validate_all() == {t: [] for t in packs.tags()} or not any(packs.validate_all().values())
    for t in packs.tags():
        p = packs.pack(t)
        for v in (p.get("tts") or {}).get("voices") or []:
            assert v.get("commercial_use") in packs.LICENCE_STATES and v.get("source_url", "").startswith("https://"), (t, v.get("id"))
        assert p["mt"].get("licence") and p["asr"].get("licence")


def test_egyptian_and_gulf_are_separate_dialect_profiles():
    msa = "هذا ليس صحيحا. لماذا سوف يكون ذلك مهما؟"
    eg, _ = packs.adapt(msa, "ar-EG")
    gu, _ = packs.adapt(msa, "ar-Gulf")
    assert eg != msa and gu != msa and eg != gu
    assert packs.dialect_density(eg, "ar-EG") > packs.dialect_density(eg, "ar-Gulf")
    assert packs.dialect_density(gu, "ar-Gulf") > packs.dialect_density(gu, "ar-EG")
    for t in ("ar-EG", "ar-Gulf"):
        p = packs.pack(t)
        assert p["needs_native_review"] is True and "not native-quality" in p["dialect"]["quality_statement"]


def test_unlicensed_voice_is_refused_unless_explicitly_allowed():
    assert packs.voice_for("de")["commercial_use"] == "yes"
    with pytest.raises(LookupError, match="commercial"):
        packs.voice_for("hi")
    v = packs.voice_for("hi", allow_noncommercial=True)
    assert "licence_warning" in v


def test_localize_without_a_cleared_voice_falls_back_to_subtitles_and_says_why(home, monkeypatch):
    from nexus import project as prj
    from nexus.timeline import ops
    monkeypatch.setattr(localize.mt, "translate", lambda lines, lang: [SAMPLES["hi"]] * len(lines))
    monkeypatch.setattr(localize, "llm_translate_lines", lambda lines, lang, context="": [f"[{lang}] {x}" for x in lines])
    called = []
    monkeypatch.setattr(localize.tts, "synthesize", lambda *a, **k: called.append(a))
    p = prj.Project.create("lang test", "edit")
    tl = {"width": 640, "height": 360, "fps": 30, "lang": "en", "clips": [ops.color_clip(4.0, narration="Solar power is clean.")],
          "audio": [], "subtitles": [], "music": None, "markers": []}
    out, review = localize.localize(p, tl, "hi", dub=True)
    assert not called                                                    # no unlicensed voice was used
    assert any("dub" in c["check"] and "not produced" in c["detail"] for c in review["checks"])
    assert out["subtitles"] and all(packs.own_share(c["text"], "hi") > 0.6 for c in out["subtitles"])


def test_cjk_captions_break_without_spaces_and_keep_reading_speed():
    text = "太阳能电池板在夏天会损失百分之二十的功率，因为热量降低了每个硅电池的输出。清洁玻璃上的灰尘可以恢复损失的电力。"
    cues = packs.segment(text, 0.0, 14.0, "zh")
    r = packs.subtitle_rules("zh")
    assert len(cues) >= 2 and all(" " not in c["text"] for c in cues)
    assert all(max(len(x) for x in c["text"].split("\n")) <= r["max_chars_per_line"] for c in cues)
    assert all(not c["over_cps"] for c in cues)
    assert all(b["start"] >= a["end"] for a, b in zip(cues, cues[1:]))
    assert "".join(c["text"].replace("\n", "") for c in cues) == text


def test_latin_segmentation_obeys_reading_speed_and_never_overlaps():
    cues = packs.segment("Solar panels lose twenty percent of their power in summer because heat reduces the output of "
                         "every silicon cell on the roof, and cleaning restores some of it.", 1.0, 9.0, "en")
    assert all(b["start"] >= a["end"] for a, b in zip(cues, cues[1:]))
    assert all(len(ln) <= 42 for c in cues for ln in c["text"].split("\n"))


def test_french_and_german_typography():
    fr = packs.typography('Il a dit "bonjour" ! Vraiment ?', "fr")
    assert "«" in fr and "»" in fr and " !" in fr and " ?" in fr
    assert packs.typography('Er sagte "Hallo".', "de") == "Er sagte „Hallo“."


def test_language_qa_flags_untranslated_overlap_and_glossary():
    cues = [{"start": 0.0, "end": 2.0, "text": "Солнечная энергия"}, {"start": 1.5, "end": 3.0, "text": "This was not translated"},
            {"start": 3.2, "end": 4.0, "text": "Панели"}]
    rep = packs.qa(cues, "ru", glossary={"battery": "аккумулятор"})
    bad = {c["id"] for c in rep["checks"] if not c["ok"]}
    assert {"script", "overlap", "glossary"} <= bad and not rep["passed"]


@pytest.mark.parametrize("tag", ["ar", "zh", "hi", "ru"])
def test_caption_png_draws_real_glyphs_for_each_script(home, tag):
    if packs.font_file(tag) is None:
        pytest.skip(f"no font for {tag} installed on this machine (reported by packs.font_file)")
    png = ttext.overlay_png(SAMPLES[tag], "subtitle" if "subtitle" in ttext.STYLES else "title", 640, 360, tag)
    from PIL import Image
    a = np.asarray(Image.open(png).convert("RGBA"))[..., 3]
    assert (a > 0).sum() > 500  # text pixels exist


@pytest.mark.parametrize("tag", ["ar-EG", "zh", "hi"])
def test_ass_burn_in_renders_non_latin_scripts(home, tmp_path, tag):
    from nexus.fx import captions
    cues = [{"start": 0.1, "end": 1.9, "text": SAMPLES[tag.split("-")[0]]}]
    ass = captions.write_ass(cues, tmp_path / "c.ass", 640, 360, tag, None)
    out = tmp_path / "o.mp4"
    esc = str(ass).replace("\\", "/").replace(":", "\\:")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x808080:s=640x360:d=2", "-vf", f"ass='{esc}'",
                    "-frames:v", "30", str(out)], check=True)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    f = np.frombuffer(raw[: 640 * 360], dtype=np.uint8).reshape(360, 640)
    assert (np.abs(f.astype(int) - 128) > 60).sum() > 300  # caption glyphs drawn over the flat grey


def test_mt_post_processing_fixes_the_errors_found_in_the_real_german_run():
    """Found by language QA on Demo 3 (MADLAD-400, German): truncation at an ordinal, a decoder loop, an added sentence."""
    from nexus.speech import mt
    assert mt._sentences("Wenn wir uns dem 24. Jahr nähern, wird es jemand erfahren.") == [
        "Wenn wir uns dem 24. Jahr nähern, wird es jemand erfahren."]
    assert len(mt._sentences("Ich hoffe, du fliegst eines Tages zum Mond… Ich wünsche dir viel Erfolg.")) == 2
    assert mt.collapse_repeats("Das ist eine lange Zeit, eine lange Zeit.") == "Das ist eine lange Zeit."
    assert mt.collapse_repeats("Wir sehen uns, wir sehen es.") == "Wir sehen uns, wir sehen es."


def test_voiceover_mode_keeps_the_original_speaker_audible_and_labels_the_synthetic_voice(home, monkeypatch, tmp_path):
    import subprocess

    from nexus import project as prj
    from nexus.timeline import ops
    wav = tmp_path / "v.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=500:d=1.5:r=48000", str(wav)], check=True)
    monkeypatch.setattr(localize.mt, "translate", lambda lines, lang: ["Solarenergie ist sauber."] * len(lines))
    monkeypatch.setattr(localize, "llm_translate_lines", lambda lines, lang, context="": lines)
    monkeypatch.setattr(localize.qa, "dub_review", lambda *a, **k: {"check": "dub", "ok": True, "severity": "minor", "detail": "stub"})

    def fake_tts(text, out, lang, voice=None, length_scale=1.0, **k):
        import shutil
        from pathlib import Path
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(wav, out)
        return {"duration": 1.5, "voice": voice, "license": "CC0", "commercial": True, "engine": "stub"}
    monkeypatch.setattr(localize.tts, "synthesize", fake_tts)
    p = prj.Project.create("vo", "edit")
    clip = ops.video_clip(str(wav), 0, 3.0, narration="Solar power is clean.")
    tl = {"width": 320, "height": 180, "fps": 30, "lang": "en", "clips": [clip], "audio": [], "subtitles": [], "music": None, "markers": []}
    out, review = localize.localize(p, tl, "de", dub=True, mode="voiceover")
    c = out["clips"][0]
    assert not c.get("mute") and 0 < c["volume"] < 0.5                       # original speaker still audible
    assert out["audio"][0]["synthetic"] and out["audio"][0]["role"] == "narration"
    assert "voice-over" in review["label"] and out["dub"]["voice"]["commercial"]


def test_translation_units_are_whole_sentences_split_back_by_word_share(monkeypatch):
    from nexus import localize as L
    seen = []
    monkeypatch.setattr(L, "translate_lines", lambda lines, lang, context="": seen.extend(lines) or [x.upper() for x in lines])
    src = ["We went down a shaft in the cave,", "and then we hiked about 300 meters and", "found a flat sandy area.", "Done."]
    out = L.translate_sentences(src, "de")
    assert seen == [" ".join(src[:3]), "Done."]                     # MT never sees a fragment
    assert " ".join(out[:3]) == " ".join(src[:3]).upper() and out[3] == "DONE."
    assert [len(x.split()) for x in out[:3]] == [8, 8, 5]           # split in proportion to each clip's words
    assert L.split_by_share("我们下到洞里然后走了很远", [1, 1]) == ["我们下到洞里", "然后走了很远"]


def test_narration_units_use_the_words_inside_the_clip():
    from nexus import localize as L
    words = [{"start": i * 0.5, "end": i * 0.5 + 0.4, "word": w} for i, w in enumerate("one two three four five six".split())]
    an = {"transcript": {"segments": [{"start": 0, "end": 3, "text": "one two three four five six", "words": words}]}}
    tl = {"clips": [{"type": "video", "src": "s.mp4", "in": 1.0, "out": 2.0}]}
    assert L.narration_units(tl, {"s.mp4": an})[0]["text"] == "three four"


def test_merged_sentence_that_comes_back_short_is_retranslated_line_by_line(monkeypatch):
    from nexus import localize as L
    def fake(lines, lang, context=""):
        return ["kurz." if len(x.split()) > 10 else "übersetzt " * len(x.split()) for x in lines]   # drops content when long
    monkeypatch.setattr(L, "translate_lines", fake)
    src = ["we went down a long shaft in the cave", "and then we found a flat sandy area."]
    out = L.translate_sentences(src, "de")
    assert all(len(o.split()) == len(s.split()) for o, s in zip(out, src))
    assert [len(g) for g in L.sentence_groups(["a " * 20, "b " * 20 + "."])] == [1, 1]   # never past GROUP_MAX_WORDS


@pytest.mark.parametrize("voice_s,expect_speed,flagged", [(3.2, 1.0, False), (5.0, 0.85, True)])
def test_voiceover_never_puts_the_speaker_in_slow_motion(home, monkeypatch, tmp_path, voice_s, expect_speed, flagged):
    """First German voice-over slowed 19 of 39 clips (down to 0.6x). Now: faster read, shorter delay, 0.5 s spill,
    then at most 0.85x with a review flag."""
    import subprocess

    from nexus import project as prj
    from nexus.timeline import ops
    wav = tmp_path / "v.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=500:d=3:r=48000", str(wav)], check=True)
    monkeypatch.setattr(localize.mt, "translate", lambda lines, lang: ["Solarenergie ist sauber und günstig."] * len(lines))
    monkeypatch.setattr(localize, "llm_translate_lines", lambda lines, lang, context="": lines)
    monkeypatch.setattr(localize.qa, "dub_review", lambda *a, **k: {"check": "dub", "ok": True, "severity": "minor", "detail": "stub"})
    scales = []

    def fake_tts(text, out, lang, voice=None, length_scale=1.0, **k):
        import shutil
        from pathlib import Path
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(wav, out)
        scales.append(length_scale)
        return {"duration": voice_s * length_scale / scales[0], "voice": voice, "license": "CC0", "commercial": True, "engine": "stub"}
    monkeypatch.setattr(localize.tts, "synthesize", fake_tts)
    p = prj.Project.create("vo2", "edit")
    clip = ops.video_clip(str(wav), 0, 3.0, narration="Solar power is clean and cheap.")
    tl = {"width": 320, "height": 180, "fps": 30, "lang": "en", "clips": [clip], "audio": [], "subtitles": [], "music": None, "markers": []}
    out, review = localize.localize(p, tl, "de", dub=True, mode="voiceover")
    assert abs(float(out["clips"][0].get("speed", 1.0)) - expect_speed) < 0.01
    assert any(c["check"].startswith("voice-over fit") and not c["ok"] for c in review["checks"]) == flagged
    assert out["audio"][0]["start"] < 0.1 + localize.VOICEOVER_DELAY + 1e-6


def test_voiceover_lines_never_overlap_even_when_one_spills_over_its_cut(home, monkeypatch, tmp_path):
    """Second German voice-over run: spill allowance produced 4 overlapping synthetic lines (critical)."""
    import subprocess

    from nexus import project as prj
    from nexus.timeline import ops
    wav = tmp_path / "v.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=500:d=3.3:r=48000", str(wav)], check=True)
    monkeypatch.setattr(localize.mt, "translate", lambda lines, lang: ["Ein ziemlich langer deutscher Satz hier."] * len(lines))
    monkeypatch.setattr(localize, "llm_translate_lines", lambda lines, lang, context="": lines)
    monkeypatch.setattr(localize.qa, "dub_review", lambda *a, **k: {"check": "dub", "ok": True, "severity": "minor", "detail": "stub"})

    def fake_tts(text, out, lang, voice=None, length_scale=1.0, **k):
        import shutil
        from pathlib import Path
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(wav, out)
        return {"duration": 3.3, "voice": voice, "license": "CC0", "commercial": True, "engine": "stub"}
    monkeypatch.setattr(localize.tts, "synthesize", fake_tts)
    p = prj.Project.create("vo3", "edit")
    clips = [ops.video_clip(str(wav), 0, 3.0, narration=f"Line {k} is a sentence.") for k in range(3)]
    tl = {"width": 320, "height": 180, "fps": 30, "lang": "en", "clips": clips, "audio": [], "subtitles": [], "music": None, "markers": []}
    out, review = localize.localize(p, tl, "de", dub=True, mode="voiceover")
    spans = sorted((a["start"], a["start"] + 3.3) for a in out["audio"])
    assert all(b[0] >= a[1] + 0.05 for a, b in zip(spans, spans[1:])), spans
