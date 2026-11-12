"""Account 3 localisation intelligence (agent D): matrix, translation QA on deliberately broken translations,
dialect profiles, dubbing manifest + consent, subtitle exporters. No models, no network."""
import pytest

from nexus.intelligence import model_router
from nexus.intelligence.contracts import validate
from nexus.intelligence.language import dialect, dubbing, matrix, subtitles
from nexus.intelligence.language import translation_qa as tq

SRC = ["The Aswan High Dam was completed in 1970 after eleven years of work.",
       "It holds back Lake Nasser, which stores about 132 cubic kilometres of water.",
       "Engineers from the Soviet Union helped design its 2.1 gigawatt power station. It still runs today."]
GOOD_DE = ["Der Assuan-Staudamm wurde 1970 nach elf Jahren Arbeit fertiggestellt.",
           "Er staut den Nassersee, der etwa 132 Kubikkilometer Wasser speichert.",
           "Ingenieure aus der Soviet Union halfen beim Entwurf des 2,1-Gigawatt-Kraftwerks. Es läuft bis heute."]
GLOSSARY_DE = {"Aswan High Dam": "Assuan-Staudamm", "Lake Nasser": "Nassersee"}


def checks(res):
    return {f["check"] for f in res["findings"] if f["severity"] != "info"}


# ------------------------------------------------------------------ matrix
def test_matrix_is_valid_and_covers_required_languages():
    assert matrix.validate() == []
    need = {"en", "de", "fr", "ar", "ar-EG", "ar-Gulf", "zh", "ru", "hi", "es", "pt", "tr", "ja", "ko", "it"}
    assert need <= set(matrix.languages())


def test_matrix_never_marks_noncommercial_models_commercial():
    for name in ("nllb-200", "mms-tts", "xtts-v2"):
        assert matrix.engine(name)["commercial_use"] == "no"
    assert matrix.engine("kokoro-82m")["commercial_use"] == "yes"
    assert matrix.commercial_voices("tr") == [] and matrix.commercial_voices("ko") == []
    assert [v["id"] for v in matrix.commercial_voices("de", installed_only=True)] == ["de_DE-thorsten-medium"]
    assert all(v["commercial_use"] != "yes" for v in matrix.language("ar-Gulf")["voices"])


# ------------------------------------------------------------------ translation QA
def test_numbers_are_normalised_across_scripts_and_separators():
    assert tq.numbers("1,200 / 1.200 / 1 200 / ١٢٠٠ / १२०० / 3,5 / 3.50") == ["1200"] * 5 + ["3.5", "3.5"]


def test_good_translation_passes():
    r = tq.check_lines(SRC, GOOD_DE, "de", GLOSSARY_DE)
    assert r["lines_needing_fix"] == [], r["counts"]


@pytest.mark.parametrize("broken, expect", [
    # dropped second sentence
    ([GOOD_DE[0], GOOD_DE[1], "Ingenieure aus der Soviet Union halfen beim Entwurf des 2,1-Gigawatt-Kraftwerks."], {"missing_sentence"}),
    # changed number
    ([GOOD_DE[0].replace("1970", "1971"), GOOD_DE[1], GOOD_DE[2]], {"number_missing", "number_added"}),
    # glossary term replaced by a literal rendering
    ([GOOD_DE[0].replace("Assuan-Staudamm", "Assuan-Hochdamm"), GOOD_DE[1], GOOD_DE[2]], {"glossary_term_missing"}),
    # protected name dropped
    ([GOOD_DE[0], GOOD_DE[1], GOOD_DE[2].replace("der Soviet Union", "dem Ausland")], {"name_missing"}),
    # invented extra sentence
    ([GOOD_DE[0] + " Er ist der größte Damm der Welt.", GOOD_DE[1], GOOD_DE[2]], {"extra_sentence"}),
    # MT repetition loop
    ([GOOD_DE[0], "Er staut den Nassersee, den Nassersee, den Nassersee, der etwa 132 Kubikkilometer Wasser speichert.", GOOD_DE[2]], {"repetition"}),
    # half the line missing
    ([GOOD_DE[0], "Er staut den Nassersee.", GOOD_DE[2]], {"too_short", "number_missing"}),
])
def test_broken_translations_are_caught(broken, expect):
    r = tq.check_lines(SRC, broken, "de", GLOSSARY_DE)
    found = set().union(*(checks(x) for x in r["lines"]))
    assert expect <= found, (expect, found)
    assert r["lines_needing_fix"]
    bad = [x for x in r["lines"] if x["status"] == "needs_fix"]
    good = [x for x in r["lines"] if x["status"] == "machine_ok"]
    assert all(b["confidence"] < g["confidence"] for b in bad for g in good)


def test_line_count_mismatch_and_duplicates():
    r = tq.check_lines(SRC, [GOOD_DE[0], GOOD_DE[0]], "de")
    assert r["document"][0]["check"] == "line_alignment"
    assert "duplicate_line" in checks(r["lines"][1])


def test_unsupported_content_and_omission_from_back_translation():
    tgt = "Der Assuan-Staudamm wurde 1970 fertiggestellt."
    back_added = "The Aswan High Dam was completed in 1970 by the British army using slave labour and foreign gold loans."
    r = tq.check_line(SRC[0], tgt, "de", back=back_added)
    assert "unsupported_content" in checks(r)
    r2 = tq.check_line(SRC[1], "Er staut den See.", "de", back="It holds back the lake.")
    assert "omission_backcheck" in checks(r2)
    ok = tq.check_line(SRC[0], GOOD_DE[0], "de", GLOSSARY_DE, back="The Aswan Dam was finished in 1970 after eleven years of work.")
    assert ok["status"] == "machine_ok" and ok["coverage"] >= 0.7


def test_arabic_target_names_need_glossary_or_back_translation():
    ar = "اكتمل السد العالي في أسوان عام 1970 بعد أحد عشر عاما من العمل."
    r = tq.check_line(SRC[0], ar, "ar")
    assert any(f["check"] == "name_unverifiable" for f in r["findings"])
    r2 = tq.check_line(SRC[0], ar, "ar", glossary={"Aswan High Dam": "السد العالي"})
    assert "glossary_term_missing" not in checks(r2)


def test_back_translate_without_provider_says_not_run(monkeypatch):
    def nope(*a, **k):
        raise model_router.NoProviderAvailable("translation", ["test: nothing"])
    monkeypatch.setattr(model_router, "complete", nope)
    back, note = tq.back_translate(["Hallo"], "de")
    assert back is None and "not run" in note


def test_manifest_contract_and_approval_workflow(tmp_path):
    res = {"de": tq.check_lines(SRC, GOOD_DE, "de", GLOSSARY_DE)}
    m = tq.build_manifest("p1", "en", SRC, res, GLOSSARY_DE)
    assert validate(m) == [] and m["approval"] == "machine_draft"
    assert not tq.commercially_ready(m, "de")[0]
    tq.review_line(m, "de", 0, "reviewer-de", "approve")
    assert m["approval"] == "in_review"
    tq.review_line(m, "de", 1, "reviewer-de", "correct", "Er staut den Nassersee, der rund 132 Kubikkilometer Wasser speichert.")
    tq.review_line(m, "de", 2, "reviewer-de", "approve")
    assert m["approval"] == "approved" and tq.commercially_ready(m, "de") == (True, [])
    assert m["lines"][1]["targets"]["de"]["machine"] == GOOD_DE[1]  # machine text kept
    with pytest.raises(ValueError):
        tq.review_line(m, "de", 0, "", "approve")
    tq.save(m, tmp_path / "m.json")


# ------------------------------------------------------------------ dialects
EG = "ده بيخلّي الطاقة الشمسية أرخص مصدر للكهربا، مش كده؟ عشان كده إحنا دلوقتي بنستخدمها أوي."
GULF = "ليش هذي الطاقة رخيصة الحين؟ وايد ناس يبون يستخدمونها، شلون ما نستخدمها؟ زين."
MSA = "لماذا أصبحت هذه الطاقة رخيصة الآن؟ لقد ارتفع عدد الذين يستخدمونها كثيراً."


def test_classifier_tells_eg_gulf_msa_apart():
    assert dialect.classify(EG)["label"] == "EG"
    assert dialect.classify(GULF)["label"] == "Gulf"
    assert dialect.classify(MSA)["label"] == "MSA"
    assert dialect.classify("الطاقة الشمسية")["label"] == "uncertain"
    s = dialect.classify(EG)["scores"]
    assert abs(sum(s.values()) - 1) < 0.01 and s["EG"] > s["Gulf"]


def test_lexical_fallback_gives_different_dialects(monkeypatch):
    src = "لماذا أصبحت هذه الطاقة رخيصة الآن؟ لا يوجد شيء مثلها، وهي جيدة جداً."
    eg = dialect.adapt_lines([src], "ar-EG", use_router=False)[0]
    gu = dialect.adapt_lines([src], "ar-Gulf", use_router=False)[0]
    assert eg["text"] != gu["text"]
    assert eg["classifier"]["label"] == "EG" and gu["classifier"]["label"] == "Gulf"
    assert "دلوقتي" in eg["text"] and "الحين" in gu["text"]   # word before "؟" is adapted (packs.adapt misses it)
    assert eg["forbidden_hits"] == [] and gu["forbidden_hits"] == []


def test_forbidden_markers_per_register():
    assert "الحين" in dialect.forbidden("ar-EG") and "دلوقتي" in dialect.forbidden("ar-Gulf")
    assert "سوف" in dialect.forbidden("ar-EG", "colloquial") and "سوف" not in dialect.forbidden("ar-EG", "semi_formal")
    assert dialect.forbidden_hits("والحين ده كويس", "ar-EG") == ["والحين"]


def test_router_adaptation_is_validated(monkeypatch):
    src = ["هذا المشروع كلف 450 مليون دولار."]
    calls = {}

    def fake(task, prompt, **kw):
        calls["task"], calls["privacy"] = task, kw["privacy"]
        return model_router.Result(text="", data={"lines": [calls.get("reply", "")]}, provider="stub", model="stub")
    monkeypatch.setattr(model_router, "complete", fake)
    calls["reply"] = "المشروع ده كلف 450 مليون دولار، مش قليل."
    r = dialect.adapt_lines(src, "ar-EG", privacy="private")[0]
    assert r["method"] == "router" and calls == {"task": "translation", "privacy": "private", "reply": calls["reply"]}
    calls["reply"] = "المشروع ده كلف 500 مليون دولار."   # number changed -> rejected, lexical fallback used
    r = dialect.adapt_lines(src, "ar-EG")[0]
    assert r["method"] == "lexical" and "numbers changed" in r["router_rejected"]["reason"]
    calls["reply"] = "This project cost 450 million dollars."
    assert dialect.adapt_lines(src, "ar-Gulf")[0]["method"] == "lexical"


def test_adaptation_survives_no_provider(monkeypatch):
    def nope(*a, **k):
        raise model_router.NoProviderAvailable("translation", ["none"])
    monkeypatch.setattr(model_router, "complete", nope)
    r = dialect.adapt_lines(["هذه الطاقة رخيصة الآن."], "ar-Gulf")[0]
    assert r["method"] == "lexical" and r["router"].startswith("no provider") and "الحين" in r["text"]


# ------------------------------------------------------------------ dubbing
def test_voice_selection_refuses_uncleared_voices():
    assert dubbing.select_voice("de")["id"] == "de_DE-thorsten-medium"
    for lang in ("tr", "ar-EG", "ar-Gulf", "ko"):
        with pytest.raises(dubbing.VoiceRefused):
            dubbing.select_voice(lang, commercial=True)
    assert dubbing.select_voice("hi")["engine"] == "kokoro-82m"  # cleared licence, but not installed here
    with pytest.raises(dubbing.VoiceRefused, match="installed"):
        dubbing.select_voice("hi", installed_only=True)
    v = dubbing.select_voice("tr", commercial=False)
    assert "licence_warning" in v


def test_cloning_requires_consent(tmp_path):
    with pytest.raises(dubbing.VoiceRefused, match="no consent"):
        dubbing.select_voice("es", clone={"engine": "openvoice-v2", "consent": None})
    doc = tmp_path / "consent.pdf"
    doc.write_bytes(b"%PDF signed consent")
    c = dubbing.consent_record("Speaker A", doc, ["es"], ["dubbing"], "2026-11-01")
    v = dubbing.select_voice("es", clone={"engine": "openvoice-v2", "consent": c})
    assert v["consent_id"] == c["consent_id"]
    with pytest.raises(dubbing.VoiceRefused, match="does not cover language"):
        dubbing.select_voice("fr", clone={"engine": "openvoice-v2", "consent": c})
    with pytest.raises(dubbing.VoiceRefused, match="not cleared"):
        dubbing.select_voice("es", clone={"engine": "xtts-v2", "consent": c})
    doc.write_bytes(b"tampered")
    with pytest.raises(dubbing.VoiceRefused, match="hash mismatch"):
        dubbing.select_voice("es", clone={"engine": "openvoice-v2", "consent": c})
    c["revoked"] = True
    assert "consent revoked" in dubbing.consent_problems(c, "es")


def test_dubbing_manifest_lines():
    m = dubbing.build_manifest([{"text": "Kurz.", "start": 0, "end": 2},
                                {"text": "Ein sehr langer Satz, der niemals in eine Sekunde passt, egal wie schnell.", "start": 2, "end": 3}], "de")
    assert m["voice"]["id"] == "de_DE-thorsten-medium" and m["lines"][0]["action"] == "ok"
    assert m["needs_shortening"] == [1] and m["lines"][1]["licence"]
    blocked = dubbing.build_manifest([{"text": "مرحبا", "start": 0, "end": 2}], "ar-EG")
    assert blocked["voice"] is None and "no commercially cleared" in blocked["blocked"]
    with pytest.raises(dubbing.VoiceRefused):
        dubbing.render_demo_line(blocked, 0, "/nonexistent.wav")


# ------------------------------------------------------------------ subtitles
def test_exporters_srt_vtt_ass(tmp_path):
    cues = subtitles.build_cues([{"text": "Der Damm wurde 1970 fertiggestellt {\\b1}.", "start": 1.0, "end": 4.5}], "de")
    srt = subtitles.to_srt(cues, "de")
    assert srt.startswith("1\n00:00:01,000 --> ")
    vtt = subtitles.to_vtt(cues, "de")
    assert vtt.startswith("WEBVTT") and "00:00:01.000 --> " in vtt
    ass = subtitles.to_ass(cues, "de")
    assert "[V4+ Styles]" in ass and "Dialogue: 0,0:00:01.00," in ass and "{\\b1}" not in ass  # no style injection


def test_rtl_and_cjk_rules():
    ar = subtitles.build_cues([{"text": "2024 كان عام GPS في مصر، مش كده؟", "start": 0, "end": 4}], "ar-EG")
    srt = subtitles.to_srt(ar, "ar-EG")
    assert all(line.startswith("\u200f") for line in srt.strip().split("\n\n")[0].split("\n")[2:])
    assert "\u2066GPS\u2069" in srt
    ass = subtitles.to_ass(ar, "ar-EG")
    assert ",178\n" in ass  # Arabic charset in the style
    zh = subtitles.build_cues([{"text": "太阳能电池板把阳光直接变成电。2024年，中国安装了更多的太阳能，总容量达到了数百吉瓦。", "start": 0, "end": 8}], "zh")
    assert all(len(x.rstrip(subtitles.NO_LINE_START)) <= 16 for c in zh for x in c["text"].split("\n"))
    assert not any(x.startswith(("，", "。")) for c in zh for x in c["text"].split("\n"))
    assert "2024" in "".join(c["text"] for c in zh).replace("\n", "")  # digits never split across lines
    assert subtitles.qa(zh, "zh")["passed"]
    assert subtitles.width("GPS 기술", "ko") == 4.0  # Latin + space count 0.5


def test_full_vs_condensed(tmp_path):
    cues = subtitles.build_cues([{"text": "So, actually, the dam really is very big and it basically holds a whole lake of water.", "start": 0, "end": 2.5}], "en")
    out = subtitles.export_all(cues, "en", tmp_path, "demo")
    assert set(out["paths"]) == {f"{p}.{e}" for p in ("full", "condensed") for e in ("srt", "vtt", "ass")}
    cond = (tmp_path / "demo.en.condensed.srt").read_text(encoding="utf-8")
    assert "actually" not in cond and "basically" not in cond
    assert out["qa_full"]["findings"]  # too fast at 17 cps: reported, not hidden
    assert out["condense_todo"]
