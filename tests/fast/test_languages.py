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
