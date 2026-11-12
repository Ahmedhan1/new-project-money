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
EMBLEM_WORDS = re.compile(r"\b(flag|flags|coat of arms|emblem|seal of|logo|insignia|banner of|علم|شعار)\b", re.I)
STOP = {"The", "A", "An", "In", "On", "At", "Of", "And", "This", "That", "It", "Its", "From", "To", "By", "With", "For", "As", "File",
        "Did", "What", "Why", "How", "When", "Where", "Who", "Today", "Built", "Designed", "Even", "Source", "Image", "Photo"}


# Words that never identify a place on their own ("Upper" matched Ghana's "Upper East", Gambia's "Upper River",
# Guyana's "Upper Takutu" and South Sudan's "Upper Nile" for the phrase "Upper Egypt" - a real false positive).
GENERIC = {"upper", "lower", "north", "south", "east", "west", "northern", "southern", "eastern", "western", "central",
           "new", "saint", "san", "santa", "greater", "inner", "outer", "middle", "grand", "great", "high", "lake", "river",
           "mount", "valley", "city", "province", "region", "state", "district", "governorate", "island", "islands", "red", "white", "blue"}


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
        if len(stem) >= 5 and _fold(first) not in GENERIC:
            idx.setdefault(stem, set()).add(cc)
    # common English exonyms that ISO lists under local names
    for name, cc in {"Egypt": "EG", "Aswan": "EG", "Cairo": "EG", "Giza": "EG", "Luxor": "EG", "Alexandria": "EG", "Nile": "EG",
                     "Lake Nasser": "EG", "Russia": "RU", "Moscow": "RU", "Belgorod": "RU", "Bavaria": "DE", "Munich": "DE",
                     "Dubai": "AE", "Abu Dhabi": "AE", "Riyadh": "SA", "Mecca": "SA", "Doha": "QA", "Kuwait": "KW", "Sudan": "SD",
                     "Khartoum": "SD", "Kourou": "GF", "Upper Egypt": "EG", "Lower Egypt": "EG", "New Valley": "EG", "Lower Nile": "EG", "Nubia": "EG", "Paris": "FR", "Berlin": "DE", "London": "GB", "Beijing": "CN", "Delhi": "IN", "Mumbai": "IN"}.items():
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


def _place_words(places: dict) -> set[str]:
    """Distinctive words of the sub-country places ("Aswan High" -> {"aswan"}; countries and generic words dropped), so
    "High Dam Burg, Aswan" matches a scene about the "Aswan High Dam" (Demo 10 run 2 refused that real photo)."""
    out = set()
    for p in places:
        if _is_country(p):
            continue
        out |= {w for w in re.findall(r"[^\W\d_]+", _fold(p)) if w not in GENERIC and len(w) > 2 and not _is_country(w)}
    return out


@functools.lru_cache(maxsize=4096)
def _is_country(name: str) -> bool:
    """A country (or a river/region spanning countries) - too broad to confirm what an image shows."""
    import pycountry
    name = re.sub(r"['’]s$", "", name)
    if _fold(name) in {"nile", "sahara", "middle east", "arabia", "europe", "africa", "asia"}:
        return True
    try:
        pycountry.countries.lookup(name)
        return True
    except LookupError:
        return _fold(name) in {_fold(n) for n in ("Egypt", "Russia", "Sudan", "Kuwait")}


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
        ws = ph.split()
        subs = [" ".join(ws[:n]) for n in range(len(ws) - 1, 1, -1)]  # "New Valley Governorate" -> "New Valley"
        for piece in [ph, *subs, *[w for w in ws if _fold(w) not in GENERIC]]:
            k = _fold(piece)
            # inflected forms only ("Belgorodskaya" -> stem "belgorod"): the INDEX key must be a prefix of the phrase.
            # The reverse direction matched "Kourou" (French Guiana) to Guinea's "Kouroussa" - a real false positive.
            ccs = idx.get(k) or next((v for kk, v in idx.items() if len(kk) >= 5 and kk not in GENERIC and k.startswith(kk)
                                      and len(k) - len(kk) <= 5), None)
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
            "is_emblem": bool(EMBLEM_WORDS.search(str(asset.get("title") or ""))),
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
    # Same country, different named place: the Giza pyramids for a scene about the Aswan High Dam (Demo 10 frames).
    scene_sub, asset_sub = _place_words(ctx["places"]), _place_words(a["places"])
    if scene_sub and asset_sub and not (scene_sub & asset_sub) and not any(x in _fold(a["meta"]) for x in scene_sub) \
            and not any(f["check"].startswith("geo_") for f in findings):
        findings.append({"check": "place_mismatch", "blocking": True,
                         "detail": f"scene names {sorted(ctx['places'])}; asset shows {sorted(a['places'])} - same country, different place"})
    if ctx["modern"] and a["years"] and max(a["years"]) < MODERN_FROM - 50 and not (ctx["years"] and min(ctx["years"]) < MODERN_FROM):
        findings.append({"check": "historical_in_modern", "blocking": True,
                         "detail": f"asset dated {max(a['years'])}, scene is modern ({ctx['years'] or 'present day'})"})
    if a["is_map"] and (a["years"] and max(a["years"]) < MODERN_FROM or a["historic_words"]) and not ctx["about_maps"]:
        findings.append({"check": "old_map", "blocking": True, "detail": "historical map used for a scene that is not about maps or history"})
    if a["is_emblem"] and not EMBLEM_WORDS.search(ctx["text"]):
        findings.append({"check": "emblem", "blocking": True,
                         "detail": "flag / coat of arms / logo used as scene footage for a scene that is not about it"})
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
