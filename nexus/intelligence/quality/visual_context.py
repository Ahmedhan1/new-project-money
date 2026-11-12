"""Visual-context gate for B-roll / stock images: geography, historical period and asset kind must fit the scene.

Found on a real render (Account 3 Demo 4): an 18th-century map titled "Belgorod governorate 1745" (Russia) illustrated
the modern Aswan Governorate (Egypt) because both contain the word "governorate". Keyword overlap is never enough for a
geographic or historical scene. The gate resolves place names to countries (ISO 3166 countries + 5,000 subdivisions from
pycountry, Wikidata as an online fallback), reads the asset's own metadata (title, description, categories, date) and
returns explainable findings:

  geo_mismatch       the asset names a place in a different country than the scene
  geo_unverified     the scene is about a named place and nothing in the asset's metadata places it there
  historical_in_modern  asset dated (or titled) long before the scene's period
  old_map            a historical map used where the scene is not about history / maps
  classified_as      optional image classification evidence (SigLIP zero-shot) when the model is available

A candidate passes only with no blocking finding. Everything is recorded so a reviewer sees why an image was refused."""
from __future__ import annotations

import functools
import json
import re
import unicodedata

from ... import config

MODERN_FROM = 1900
MAP_WORDS = re.compile(r"\b(map|maps|karte|carte|mapa|kaart|atlas|cartograph\w*|خريطة|карта)\b", re.I)
HISTORIC_WORDS = re.compile(r"\b(engraving|lithograph|woodcut|etching|antique|ancient|medieval|historic(al)?|governorate of the russian empire)\b", re.I)
STOP = {"The", "A", "An", "In", "On", "At", "Of", "And", "This", "That", "It", "Its", "From", "To", "By", "With", "For", "As", "File",
        "Did", "What", "Why", "How", "When", "Where", "Who", "Today", "Built", "Designed", "Even", "Source", "Image", "Photo"}


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower().replace("'", "").replace("’", "")


@functools.lru_cache(maxsize=1)
def _index() -> dict[str, set[str]]:
    """folded name/prefix -> country codes. Countries by all their names; subdivisions by name and stem."""
    import pycountry
    idx: dict[str, set[str]] = {}

    def add(name: str, cc: str):
        k = _fold(name).strip()
        if len(k) >= 4:
            idx.setdefault(k, set()).add(cc)

    for c in pycountry.countries:
        for n in {c.name, getattr(c, "official_name", c.name), getattr(c, "common_name", c.name)}:
            add(n, c.alpha_2)
    for s in pycountry.subdivisions:
        cc = s.country_code
        base = re.sub(r"\(.*?\)", "", s.name).strip()
        add(base, cc)
        first = base.split()[0] if base.split() else base
        stem = re.sub(r"(skaya|skiy|skij|skaja|ska|sko|ia|iya)$", "", _fold(first))
        if len(stem) >= 5:
            idx.setdefault(stem, set()).add(cc)
    # common English exonyms that ISO lists under local names
    for name, cc in {"Egypt": "EG", "Aswan": "EG", "Cairo": "EG", "Giza": "EG", "Luxor": "EG", "Alexandria": "EG", "Nile": "EG",
                     "Lake Nasser": "EG", "Russia": "RU", "Moscow": "RU", "Belgorod": "RU", "Bavaria": "DE", "Munich": "DE",
                     "Dubai": "AE", "Abu Dhabi": "AE", "Riyadh": "SA", "Mecca": "SA", "Doha": "QA", "Kuwait": "KW", "Sudan": "SD",
                     "Khartoum": "SD", "Paris": "FR", "Berlin": "DE", "London": "GB", "Beijing": "CN", "Delhi": "IN", "Mumbai": "IN"}.items():
        idx.setdefault(_fold(name), set()).add(cc)
    return idx


def _wikidata_country(name: str) -> set[str]:
    """Online fallback: Wikidata entity search -> country (P17) ISO code (P297). Cached; empty when unreachable."""
    cache = config.CACHE / "wikidata_places.json"
    data = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    if name in data:
        return set(data[name])
    out: set[str] = set()
    try:
        from ... import net
        s = net.get_json("https://www.wikidata.org/w/api.php", {"action": "wbsearchentities", "search": name, "language": "en",
                                                                 "format": "json", "limit": 1, "type": "item"}, retries=1)
        if s.get("search"):
            qid = s["search"][0]["id"]
            e = net.get_json("https://www.wikidata.org/w/api.php", {"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"}, retries=1)
            for c in e["entities"][qid]["claims"].get("P17", []):
                cq = c["mainsnak"]["datavalue"]["value"]["id"]
                ce = net.get_json("https://www.wikidata.org/w/api.php", {"action": "wbgetentities", "ids": cq, "props": "claims", "format": "json"}, retries=1)
                for x in ce["entities"][cq]["claims"].get("P297", []):
                    out.add(x["mainsnak"]["datavalue"]["value"])
    except Exception:
        return set()
    data[name] = sorted(out)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return out


def candidates(text: str) -> list[str]:
    """Proper-noun phrases: runs of capitalised words (keeps 'Aswan High Dam', 'Lake Nasser', 'Belgorod')."""
    out = []
    for m in re.finditer(r"\b([A-Z][\w'’\-]+(?:\s+(?:of\s+)?[A-Z][\w'’\-]+)*)", text or ""):
        words = [w for w in m.group(1).split() if w not in STOP]
        if words:
            out.append(" ".join(words))
    return out


def places(text: str, online: bool = False) -> dict[str, list[str]]:
    """{place phrase: [country codes]} for every phrase (or word inside it) that resolves to a country/region."""
    idx = _index()
    found: dict[str, set[str]] = {}
    for ph in candidates(text):
        for piece in [ph, *ph.split()]:
            k = _fold(piece)
            ccs = idx.get(k) or next((v for kk, v in idx.items() if len(k) >= 6 and kk.startswith(k[:6]) and len(kk) <= len(k) + 6), None)
            if ccs:
                found.setdefault(piece, set()).update(ccs)
                break
        else:
            if online and len(ph) >= 4:
                ccs = _wikidata_country(ph)
                if ccs:
                    found.setdefault(ph, set()).update(ccs)
    return {k: sorted(v) for k, v in found.items()}


def years(text: str) -> list[int]:
    return [int(y) for y in re.findall(r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)", text or "")]


def scene_context(topic: str, narration: str = "", visual: str = "", online: bool = False) -> dict:
    t = " ".join([topic or "", narration or "", visual or ""])
    ys = years(t)
    return {"places": places(t, online), "years": ys, "modern": (min(ys) >= MODERN_FROM) if ys else not HISTORIC_WORDS.search(t),
            "about_maps": bool(MAP_WORDS.search(t)), "text": t[:400]}


def asset_profile(asset: dict, online: bool = False) -> dict:
    meta = " ".join(str(asset.get(k) or "") for k in ("title", "description", "categories", "date", "object_name"))
    ys = years(" ".join(str(asset.get(k) or "") for k in ("title", "date", "description")))
    return {"places": places(meta, online), "years": ys, "is_map": bool(MAP_WORDS.search(meta)),
            "historic_words": bool(HISTORIC_WORDS.search(meta)), "meta": meta[:400]}


def check(asset: dict, ctx: dict, online: bool = False, classify: bool = False, image_path: str | None = None) -> dict:
    a = asset_profile(asset, online)
    findings = []
    scene_cc = {c for v in ctx["places"].values() for c in v}
    asset_cc = {c for v in a["places"].values() for c in v}
    if scene_cc and asset_cc and not (scene_cc & asset_cc):
        findings.append({"check": "geo_mismatch", "blocking": True,
                         "detail": f"scene is in {sorted(scene_cc)} ({', '.join(ctx['places'])}); asset names {sorted(asset_cc)} ({', '.join(a['places'])})"})
    elif scene_cc and not asset_cc:
        named = [p for p in ctx["places"] if _fold(p) in _fold(a["meta"])]
        if not named:
            findings.append({"check": "geo_unverified", "blocking": True,
                             "detail": f"scene is about {', '.join(ctx['places'])}; nothing in the asset's title/description/categories places it there"})
    if ctx["modern"] and a["years"] and max(a["years"]) < MODERN_FROM - 50 and not (ctx["years"] and min(ctx["years"]) < MODERN_FROM):
        findings.append({"check": "historical_in_modern", "blocking": True,
                         "detail": f"asset dated {max(a['years'])}, scene is modern ({ctx['years'] or 'present day'})"})
    if a["is_map"] and (a["years"] and max(a["years"]) < MODERN_FROM or a["historic_words"]) and not ctx["about_maps"]:
        findings.append({"check": "old_map", "blocking": True, "detail": "historical map used for a scene that is not about maps or history"})
    if classify and image_path:
        cls = classify_image(image_path, ctx.get("subject") or ctx["text"][:60])
        if cls:
            findings.append({"check": "classified_as", "blocking": cls["label"] in ("an old map", "a logo or emblem") and not ctx["about_maps"],
                             "detail": f"zero-shot: {cls['label']} ({cls['score']:.2f})", "scores": cls["scores"]})
    return {"ok": not any(f["blocking"] for f in findings), "findings": findings, "asset": {k: a[k] for k in ("places", "years", "is_map")},
            "scene": {k: ctx[k] for k in ("places", "years", "modern")}}


_clf: dict = {}


def classify_image(path: str, subject: str) -> dict | None:
    """SigLIP zero-shot (Apache-2.0, google/siglip-base-patch16-224) when it is installed; None otherwise (never downloads
    during a render unless NEXUS_ALLOW_MODEL_DOWNLOAD=1)."""
    import os
    try:
        if "m" not in _clf:
            from transformers import pipeline
            kw = {} if os.environ.get("NEXUS_ALLOW_MODEL_DOWNLOAD") == "1" else {"model_kwargs": {"local_files_only": True}}
            _clf["m"] = pipeline("zero-shot-image-classification", model="google/siglip-base-patch16-224", **kw)
        labels = [f"a photograph of {subject}", "an old map", "a diagram or chart", "a painting or engraving", "a logo or emblem", "a portrait of a person"]
        r = _clf["m"](path, candidate_labels=labels)
        return {"label": r[0]["label"], "score": float(r[0]["score"]), "scores": {x["label"]: round(float(x["score"]), 3) for x in r}}
    except Exception:
        return None
