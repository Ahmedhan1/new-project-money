"""Caption cue regressions found on the 4-minute acceptance edit (session 6)."""
from nexus.timeline import subs


def _w(t0, words, step=0.3):
    return [{"start": round(t0 + i * step, 3), "end": round(t0 + i * step + 0.25, 3), "word": w} for i, w in enumerate(words)]


def test_a_cue_never_spans_a_title_card_or_a_cut():
    # "...flat area." ends at 18.4 s, a 3 s title card follows, the next clip starts at 21.5 s
    words = _w(17.0, "relatively flat area.".split()) + _w(21.6, "long before you find out".split())
    cues = subs.cues_from_transcript([{"start": 0, "end": 0, "text": "", "words": words}], breaks=[18.53, 21.53])
    assert [c["text"] for c in cues] == ["relatively flat area.", "long before you find out"]
    assert cues[0]["end"] <= 18.53 and cues[1]["start"] >= 21.53


def test_a_sentence_never_shares_a_cue_with_the_next_one():
    words = _w(0, "That is rare in caves. So we looked".split())
    cues = subs.cues_from_transcript([{"start": 0, "end": 0, "text": "", "words": words}])
    assert cues[0]["text"] == "That is rare in caves."


def test_whisper_split_hyphen_tokens_are_glued():
    words = _w(0, ["a", "160", "-meter", "shaft"])
    assert subs.cues_from_transcript([{"start": 0, "end": 0, "text": "", "words": words}])[0]["text"] == "a 160-meter shaft"


def test_fast_cues_get_reading_time_from_following_silence_but_not_across_a_cut():
    cues = subs.fix_timing([{"start": 0.0, "end": 1.0, "text": "x" * 30}, {"start": 3.0, "end": 4.0, "text": "y" * 30}],
                           breaks=[1.2])
    assert abs(cues[0]["end"] - 1.2) < 1e-6                     # wants 1.76 s, the cut at 1.2 s stops it
    assert abs(cues[1]["end"] - 4.5) < 1e-6                     # free silence after: lingers up to 0.5 s


def test_a_word_on_a_cut_is_captioned_once():
    from nexus import edit
    words = _w(0, "so I would suspect you know as we get closer".split())
    an = {"transcript": {"segments": [{"start": 0, "end": 3, "text": "", "words": words}]}}
    cut = (words[6]["start"] + words[6]["end"]) / 2 - 0.05          # just before the midpoint of "as"
    tl = {"clips": [{"type": "video", "src": "s", "in": 0.0, "out": cut}, {"type": "video", "src": "s", "in": cut, "out": 3.3}],
          "subtitles": []}
    text = " ".join(c["text"] for c in edit.retime_subtitles(tl, {"s": an})["subtitles"])
    assert text.split().count("as") == 1, text


def test_whisper_hyphen_tokens_are_merged_in_the_transcript():
    from nexus.speech import asr
    seg = {"start": 0, "end": 2, "text": "a 160 -meter shaft and T -38 jets - fast",
           "words": [{"start": 0.0, "end": 0.2, "word": "a"}, {"start": 0.3, "end": 0.6, "word": "160"},
                     {"start": 0.6, "end": 0.9, "word": "-meter"}, {"start": 1.0, "end": 1.2, "word": "shaft"}]}
    n = asr.normalize_segment(seg)
    assert n["text"] == "a 160-meter shaft and T-38 jets - fast"          # a free-standing dash is left alone
    assert [w["word"] for w in n["words"]] == ["a", "160-meter", "shaft"] and n["words"][1]["end"] == 0.9
