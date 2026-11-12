"""LANG-1 fast tests: language registry, script checks, Han cue splitting, numerals, dialect markers, same-language refusal.
No models, no network: everything here is registry data and pure text logic."""
import pytest

from nexus import config, models
from nexus.speech import asr, mt
from nexus.timeline import subs

PRIORITY = ["en", "de", "fr", "ar-EG", "ar-GULF", "zh", "ru", "hi"]
REQUIRED = PRIORITY + ["ar", "es", "pt-BR", "it", "tr", "id"]
FIELDS = ["name", "native", "script", "direction", "whisper", "tts", "espeak", "pace", "font", "numerals", "length_ratio",
          "cue_chars", "dub_threshold", "translation", "priority", "status", "reason"]


def test_registry_complete_and_consistent():
    langs = models.languages()
    assert len(langs) >= 12 and all(c in langs for c in REQUIRED)
    assert list(langs)[:len(PRIORITY)] == PRIORITY                                  # owner's priority order
    voices = models.catalog()["voices"]
    for code, e in langs.items():
        missing = [f for f in FIELDS if f not in e]
        assert not missing, (code, missing)
        assert e["status"] in models.STATUSES and e["reason"].strip()
        assert e["script"] in models.SCRIPT_RANGES and e["script"] in models._lang_file()["scripts"]
        assert e["direction"] == ("rtl" if e["script"] == "arabic" else "ltr")
        assert e["font"] in models._lang_file()["fonts"]
        r = e["length_ratio"]
        assert 0 < r["min"] < r["typical"] < r["max"]
        assert 0.3 <= e["dub_threshold"] <= 0.95 and 0.5 <= e["pace"] <= 1.5
        if e["status"] != "unavailable":
            # translatable: a MADLAD tag, or a pivot language that has one
            assert e.get("madlad") or langs[e["msa_pivot"]].get("madlad"), code
            # dub: a Piper voice from the catalogue, or eSpeak NG (always available, clear licence)
            assert e["voice"] is None or e["voice"] in voices, code
            assert e["espeak"]
        if e["status"] == "available":
            assert e["voice"] and voices[e["voice"]]["commercial"], f"{code}: 'available' needs a commercially cleared voice"
        if e["voice"] and not voices[e["voice"]]["commercial"]:
            assert e["status"] != "available", code
        if e.get("dialect"):
            assert e["dialect"] in models._lang_file()["dialect_markers"]


def test_gulf_is_never_msa_in_disguise():
    g = models.language("ar-GULF")
    assert g["madlad"] is None and g["msa_pivot"] == "ar"            # no Gulf tag exists in MADLAD-400 (verified)
    assert g["status"] != "available" or g.get("dialect") == "ar-GULF"
    assert models.resolve_lang("afb") == "ar-GULF" and models.resolve_lang("arz") == "ar-EG"


def test_voice_selection_follows_registry():
    assert models.voice_for("de") == "de_DE-thorsten-medium"
    assert models.voice_for("ar-EG") == "ar_JO-kareem-medium"
    for code in ("hi", "tr", "zh"):          # no cleared Piper voice -> KeyError -> tts falls back to eSpeak NG
        with pytest.raises(KeyError):
            models.voice_for(code)
    rejected = models.catalog()["rejected_voices"]
    assert "tr_TR-dfki-medium" in rejected and "hi_IN-pratham-medium" in rejected
    assert all("card" in v and "license" in v for v in models.catalog()["voices"].values())   # licence evidence recorded


@pytest.mark.parametrize("lang,good,bad", [
    ("de", "Größere Batterien lösen das Problem.", "Größere Batterien решают das Problem."),
    ("ru", "Аккумуляторы решают эту проблему.", "Аккумуляторы solve эту проблему."),
    ("ar", "البطاريات تحل هذه المشكلة.", "البطاريات تحل this المشكلة."),
    ("hi", "बैटरी इस समस्या का समाधान कर रही हैं।", "बैटरी इस problem का समाधान कर रही हैं।"),
    ("zh", "电池正在解决这个问题。", "电池正在解决这个problem。"),
])
def test_script_checks_per_script(lang, good, bad):
    from nexus import localize
    assert localize.foreign_words(good, lang) == []
    assert localize.foreign_words(bad, lang)
    assert localize.script_share(good, lang) > 0.95
    assert localize.foreign_words("CO2-Speicher mit AI", "de") == []
    assert localize.foreign_words("电池和AI", "zh") == []              # short Latin acronyms are allowed in any script


def test_numbers_preserved_across_numeral_systems():
    assert mt.numbers("In the last 10 years prices fell by almost 90%.") == {"10", "90"}
    assert mt.numbers("in the last ten years ... ninety percent; sixteen thousand homes") >= {"10", "90", "16000"}
    assert mt.numbers("خلال ١٠ سنوات انخفض السعر ٩٠٪", "ar") == {"10", "90"}            # Arabic-Indic digits
    assert mt.numbers("पिछले १० वर्षों में ९०% गिर गई", "hi") == {"10", "90"}            # Devanagari digits
    assert mt.numbers("在过去的１０年里下降了90%", "zh") == {"10", "90"}                  # full-width digits
    assert mt.numbers("16 000 maisons", "fr") == mt.numbers("16.000 Häuser", "de") == mt.numbers("16,000 homes")
    # MADLAD's measured failure mode: an invented clause with a new number
    src = "In the last 10 years the price of solar power fell by almost 90%."
    assert mt.added_numbers(src, "Au cours des 10 dernières années, le prix a chuté de près de 90 %, soit une baisse d'environ 25%.", "en", "fr") == {"25"}
    assert not mt.added_numbers(src, "在过去的10年里，太阳能发电价格下降了近90%。", "en", "zh")


def test_length_guards_use_characters_for_han():
    src = "Solar panels turn sunlight directly into electricity."
    zh = "太阳能电池板将阳光直接转化为电力。"
    assert not mt.too_long(src, zh, "zh") and not mt.too_short(src, zh, "zh")
    assert mt.too_short(src, "电力。", "zh")
    assert mt.too_long(src, zh * 3, "zh")
    assert not mt.too_long(src, "Солнечные батареи преобразуют солнечный свет непосредственно в электроэнергию.", "ru")
    assert mt._sentences("太阳能很便宜。电池更快！") == ["太阳能很便宜。", "电池更快！"]
    assert mt._sentences("सौर पैनल सस्ते हैं। बैटरी तेज़ है।") == ["सौर पैनल सस्ते हैं।", "बैटरी तेज़ है।"]


def test_han_cue_splitting():
    text = "在过去的10年里，太阳能发电价格下降了近90%。这使其成为大多数国家最便宜的新电力来源。电池正在比任何人预期的更快地解决这个问题。"
    cues = subs.cues_from_text(text, 0.0, 12.0, lang="zh")
    texts = [c["text"] for c in cues]
    assert "".join(texts) == text and " " not in "".join(texts)
    assert all(len(t) <= subs.HAN_CUE_CHARS + 1 for t in texts) and len(texts) >= 4
    assert not any(t[0] in "。，" for t in texts)                       # no cue starts with closing punctuation
    assert any(t.endswith("。") for t in texts[:-1])                     # sentence ends break cues
    assert "10" in "".join(texts) and not any(t.endswith("1") and texts[i + 1].startswith("0") for i, t in enumerate(texts[:-1]))
    # auto-detected without lang, and the default 42-char limit is not applied to Han
    assert [c["text"] for c in subs.cues_from_text(text, 0, 12)] == texts
    words = [{"start": i * 0.2, "end": i * 0.2 + 0.2, "word": ch} for i, ch in enumerate("太阳能电池板将阳光直接转化为电力。这使其成为最便宜的来源。")]
    tc = subs.cues_from_transcript([{"start": 0, "end": 8, "text": "", "words": words}], lang="zh")
    assert all(" " not in c["text"] and len(c["text"]) <= subs.HAN_CUE_CHARS + 1 for c in tc)


def test_rtl_and_spaced_cues_unchanged():
    ar = "في السنوات العشر الماضية، انخفض سعر الطاقة الشمسية بما يقرب من 90 في المائة. وهذا يجعلها أرخص مصدر."
    cues = subs.cues_from_text(ar, 0, 8, lang="ar")
    assert " ".join(c["text"] for c in cues) == ar and all(len(c["text"]) <= 42 for c in cues)
    hi = "सौर ऊर्जा अब बहुत सस्ती हो गई है। बैटरी तेज़ी से समस्या हल कर रही हैं।"
    hc = subs.cues_from_text(hi, 0, 8, lang="hi")
    assert " ".join(c["text"] for c in hc) == hi and not any("। " in c["text"] for c in hc)   # no cue straddles a danda
    assert hc[0]["text"].endswith("।") and len(hc) == 2


def test_ass_uses_script_font(tmp_path):
    p = subs.write_ass([{"start": 0, "end": 2, "text": "电池正在解决这个问题。"}], tmp_path / "zh.ass", 1280, 720)
    assert f"Style: Default,{config.font_name_for('zh')}," in p.read_text(encoding="utf-8")
    p = subs.write_ass([{"start": 0, "end": 2, "text": "बैटरी"}], tmp_path / "hi.ass", 1280, 720, "hi")
    assert f"Style: Default,{config.font_name_for('hi')}," in p.read_text(encoding="utf-8")


def test_fonts_contain_their_script():
    # On machines without the fonts (Windows CI) the fallback is the Latin font; where a candidate exists it must render the script.
    for lang, script in (("zh", "han_sc"), ("hi", "devanagari"), ("ar", "arabic"), ("ru", "cyrillic")):
        path, idx, family = config.font_face_for(lang)
        assert config.font_for(lang) == path and config.font_name_for(lang) == family
        sample = models._lang_file()["fonts"][script]["sample"]
        if path != config.FONT_LATIN or script == "cyrillic":
            assert config._has_glyphs(path, idx, sample), (lang, path)
    if config.font_for("zh") == config.FONT_LATIN:
        assert not config._has_glyphs(config.FONT_LATIN, 0, "汉语")    # the tofu detector itself works


def test_gulf_marker_check():
    from nexus import localize
    gulf = "الحين نحتاج حل أحسن. ليش هالمشكلة كبيرة وايد؟ هذا مو سهل، بس يشتغل."
    msa = "نحن الآن بحاجة إلى حل أفضل. لماذا هذه المشكلة كبيرة جدا؟ هذا ليس سهلاً، لكنه يعمل."
    masri = "دلوقتي محتاجين حل أحسن. ليه المشكلة دي كبيرة أوي؟ ده مش سهل، بس بيشتغل."
    assert localize.dialect_ok(gulf, "ar-GULF")
    assert not localize.dialect_ok(msa, "ar-GULF")
    assert not localize.dialect_ok(masri, "ar-GULF")                      # Egyptian is not Gulf
    assert localize.dialect_ok(masri, "ar-EG") and not localize.dialect_ok(msa, "ar-EG")


def test_language_review_flags_invented_numbers_and_gulf_msa():
    from nexus import localize
    src = ["In the last 10 years the price of solar power fell by almost 90%."]
    rv = localize.language_review(src, ["Au cours des 10 dernières années, le prix a chuté de près de 90 %, soit une baisse de 25%."], "fr")
    bad = {c["check"] for c in rv["checks"] if not c["ok"]}
    assert "no invented numbers [fr]" in bad and not rv["passed"]
    rv = localize.language_review(src, ["وفي السنوات العشر الماضية، انخفض سعر الطاقة الشمسية بما يقرب من 90 في المائة."], "ar-GULF")
    assert any(c["check"] == "Gulf dialect markers" and not c["ok"] for c in rv["checks"])
    rv = localize.language_review(src, ["在过去的10年里，太阳能发电价格下降了近90%。"], "zh")
    assert rv["passed"], [c for c in rv["checks"] if not c["ok"]]
    assert any(c["check"] == "native-speaker review [zh]" and not c["ok"] for c in rv["checks"])   # always required for MT


def test_same_language_refusal():
    assert asr.same_language("en", "en-US") and asr.same_language("pt", "pt-BR")
    assert not asr.same_language("ar", "ar-EG")                         # MSA -> Egyptian is a real adaptation
    with pytest.raises(ValueError):
        asr.check_translation_pair("de", "de")
    with pytest.raises(ValueError):                                    # declared English, Whisper is sure it is German
        asr.check_translation_pair("en", "de", {"code": "de", "whisper": "de", "probability": 0.98})
    asr.check_translation_pair("en", "de", {"code": "de", "whisper": "de", "probability": 0.3})   # not confident: declared wins
    asr.check_translation_pair("en", "ar-EG", {"code": "ar", "whisper": "ar", "probability": 0.99, "dialect_unknown": True})
    assert asr.whisper_code("ar-GULF") == "ar" and asr.whisper_code("pt-BR") == "pt" and asr.registry_code_for_whisper("zh") == "zh"


def test_unavailable_language_is_refused(home):
    from nexus import localize
    with pytest.raises(localize.LanguageUnavailable):
        localize.localize(None, {"clips": []}, "ar-GULF")
