"""Speaker diarisation on CPU with no model download and no account (DIA-1).

Method (numpy only): every transcript segment gets a voice fingerprint = mean and standard deviation of 13 MFCCs +
spectral centroid/flatness over its speech (file-level mean/variance normalised); segments are clustered by average-
linkage agglomerative clustering on cosine distance; the number of speakers (1..max) is chosen by silhouette score and
a single speaker is assumed when no split is clearly better. This is weaker than neural speaker embeddings (ECAPA,
pyannote) and assumes a transcript segment holds one speaker - it is reported with per-turn confidence, never as exact.

    diarize(path, analysis, max_speakers=4) -> analysis["speakers"] (contract in docs/MASTER_ROADMAP.md) and labels
    roles(...)            interviewer / guest / narrator / unknown with confidence and the evidence used
    rename / merge / reassign_turn   manual corrections; the original automatic result is kept under "auto"
"""
from __future__ import annotations

import copy
import re
import subprocess

import numpy as np

from .. import costguard

SR = 16000
MIN_SPEAKER_DIST = 0.25  # cosine distance between speaker centroids below which a split is not accepted
SECOND = {"you", "your", "you're", "yours", "yourself", "you've", "you'll"}


def _audio(path) -> np.ndarray:
    costguard.authorize("ffmpeg", "diarize-audio")
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True, timeout=3600).stdout
    return np.frombuffer(raw, dtype=np.float32)


def _mel_fb(n_fft: int = 512, n_mels: int = 26) -> np.ndarray:
    def hz2mel(h):
        return 2595 * np.log10(1 + h / 700)

    def mel2hz(m):
        return 700 * (10 ** (m / 2595) - 1)
    pts = mel2hz(np.linspace(hz2mel(60), hz2mel(SR / 2 - 200), n_mels + 2))
    bins = np.floor((n_fft + 1) * pts / SR).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1))
    for m in range(1, n_mels + 1):
        a, b, c = bins[m - 1], bins[m], bins[m + 1]
        if b > a:
            fb[m - 1, a:b] = (np.arange(a, b) - a) / (b - a)
        if c > b:
            fb[m - 1, b:c] = (c - np.arange(b, c)) / (c - b)
    return fb


_FB = _mel_fb()
_DCT = np.cos(np.pi / 26 * (np.arange(26)[None, :] + 0.5) * np.arange(13)[:, None])


def frame_features(x: np.ndarray) -> np.ndarray:
    """[n_frames, 15]: 13 MFCC + spectral centroid + flatness, 25 ms windows, 10 ms hop, speech frames only."""
    win, hop = 400, 160
    if len(x) < win:
        return np.zeros((0, 15))
    n = 1 + (len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    fr = x[idx] * np.hamming(win)
    spec = np.abs(np.fft.rfft(fr, 512)) ** 2 + 1e-10
    energy = 10 * np.log10(spec.sum(1))
    keep = energy > np.percentile(energy, 30)  # drop pauses/breaths: they carry the room, not the voice
    spec = spec[keep]
    if not len(spec):
        return np.zeros((0, 15))
    mel = np.log(spec @ _FB.T)
    mfcc = mel @ _DCT.T
    freqs = np.linspace(0, SR / 2, spec.shape[1])
    cen = (spec * freqs).sum(1) / spec.sum(1) / (SR / 2)
    flat = np.exp(np.log(spec).mean(1)) / spec.mean(1)
    return np.column_stack([mfcc, cen, flat])


def embed(x: np.ndarray, segs: list[dict]) -> tuple[np.ndarray, list[int]]:
    feats, keep = [], []
    allf = frame_features(x)
    mu, sd = (allf.mean(0), allf.std(0) + 1e-6) if len(allf) else (0, 1)
    for i, s in enumerate(segs):
        a, b = int(float(s["start"]) * SR), int(float(s["end"]) * SR)
        f = frame_features(x[a:b])
        if len(f) < 30:  # < ~0.3 s of speech: too little to fingerprint
            continue
        f = (f - mu) / sd
        feats.append(np.concatenate([f.mean(0), f.std(0)]))
        keep.append(i)
    return (np.array(feats) if feats else np.zeros((0, 30))), keep


# ---------------------------------------------------------------- neural speaker embeddings (WeSpeaker ResNet34, ONNX)
EMB_URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx"
EMB_LICENCE = "WeSpeaker voxceleb_resnet34_LM: CC BY 4.0 (attribution: WeSpeaker, wenet-e2e) - read 2026-11-12"
_FB80 = None
_SESS = None


def emb_model_path():
    from .. import config
    return config.MODELS / "speaker" / "voxceleb_resnet34_LM.onnx"


def ensure_emb_model():
    p = emb_model_path()
    if p.exists() and p.stat().st_size > 1_000_000:
        return p
    try:
        from .. import net
        costguard.authorize("wespeaker", "download")
        net.download(EMB_URL, p, timeout=180, retries=2)
        return p
    except Exception:  # noqa: BLE001 - offline: MFCC fallback
        return None


def fbank80(x: np.ndarray) -> np.ndarray:
    """Kaldi-style 80-bin log mel filterbank (25 ms / 10 ms, pre-emphasis 0.97, Povey window, 16-bit scale) + CMN."""
    global _FB80
    if _FB80 is None:
        _FB80 = _mel_fb(512, 80)
    win, hop = 400, 160
    x = x * 32768.0
    if len(x) < win:
        return np.zeros((0, 80), dtype=np.float32)
    n = 1 + (len(x) - win) // hop
    fr = x[np.arange(win)[None, :] + hop * np.arange(n)[:, None]].astype(np.float64)
    fr -= fr.mean(1, keepdims=True)
    fr[:, 1:] -= 0.97 * fr[:, :-1]
    fr[:, 0] *= 0.03
    povey = np.power(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(win) / (win - 1)), 0.85)
    spec = np.abs(np.fft.rfft(fr * povey, 512)) ** 2
    f = np.log(np.maximum(spec @ _FB80.T, 1.1920929e-07))
    return (f - f.mean(0)).astype(np.float32)


def embed_neural(x: np.ndarray, segs: list[dict]) -> tuple[np.ndarray, list[int]]:
    global _SESS
    import onnxruntime as ort
    if _SESS is None:
        _SESS = ort.InferenceSession(str(emb_model_path()), providers=["CPUExecutionProvider"])
    feats, keep = [], []
    for i, s in enumerate(segs):
        a, b = int(float(s["start"]) * SR), int(float(s["end"]) * SR)
        if b - a < int(0.8 * SR):  # < 0.8 s: not enough voice for a reliable embedding
            continue
        fb = fbank80(x[a:b])
        e = _SESS.run(None, {"feats": fb[None]})[0][0]
        feats.append(e / (np.linalg.norm(e) + 1e-9))
        keep.append(i)
    return (np.array(feats) if feats else np.zeros((0, 256))), keep


def _cos_dist(E: np.ndarray) -> np.ndarray:
    n = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    return np.clip(1 - n @ n.T, 0, 2)


def agglomerative(D: np.ndarray, k: int) -> np.ndarray:
    """Average-linkage clustering of a distance matrix into k clusters."""
    clusters = [[i] for i in range(len(D))]
    while len(clusters) > k:
        best, pair = 9e9, (0, 1)
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                d = D[np.ix_(clusters[i], clusters[j])].mean()
                if d < best:
                    best, pair = d, (i, j)
        i, j = pair
        clusters[i] += clusters.pop(j)
    lab = np.zeros(len(D), dtype=int)
    for c, members in enumerate(clusters):
        lab[members] = c
    return lab


def silhouette(D: np.ndarray, lab: np.ndarray) -> float:
    ks = set(lab.tolist())
    if len(ks) < 2:
        return 0.0
    s = []
    for i in range(len(D)):
        same = lab == lab[i]
        a = D[i, same & (np.arange(len(D)) != i)].mean() if same.sum() > 1 else 0.0
        b = min(D[i, lab == k].mean() for k in ks if k != lab[i])
        s.append((b - a) / max(a, b, 1e-9))
    return float(np.mean(s))


def _cluster(E: np.ndarray, max_speakers: int) -> tuple[np.ndarray, float, int]:
    D = _cos_dist(E)
    if len(E) > 220:  # agglomerative is O(n^3) here: cluster a sample, then assign everything to the nearest centroid
        idx = np.linspace(0, len(E) - 1, 220).astype(int)
        lab_s, sil, k = _cluster(E[idx], max_speakers)
        cents = np.array([E[idx][lab_s == c].mean(0) for c in range(k)])
        return np.argmin(_cos_dist(np.vstack([E, cents]))[: len(E), len(E):], axis=1), sil, k
    best = (np.zeros(len(E), dtype=int), 0.0, 1)
    for k in range(2, max(2, min(max_speakers, len(E) - 1)) + 1):
        lab = agglomerative(D, k)
        if min(np.bincount(lab)) < max(2, 0.03 * len(E)):  # a cluster of a few stray segments is noise, not a speaker
            continue
        sil = silhouette(D, lab)
        cents = np.array([E[lab == c].mean(0) for c in range(k)])
        cd = _cos_dist(cents)[np.triu_indices(k, 1)]
        if cd.min() < MIN_SPEAKER_DIST:  # clusters too similar: one voice split by noise, not two people
            continue
        if sil > best[1] + 0.02:
            best = (lab, sil, k)
    return best if best[1] >= 0.08 else (np.zeros(len(E), dtype=int), best[1], 1)


def roles(segs: list[dict], labels: dict[int, str]) -> dict:
    """Interviewer vs guest from evidence: share of questions, second-person words, talk time and turn length."""
    stats: dict[str, dict] = {}
    for i, sp in labels.items():
        s = segs[i]
        st = stats.setdefault(sp, {"seconds": 0.0, "segments": 0, "questions": 0, "you": 0, "words": 0})
        txt = s.get("text", "")
        toks = re.findall(r"[\w']+", txt.lower())
        st["seconds"] += float(s["end"]) - float(s["start"])
        st["segments"] += 1
        st["questions"] += txt.strip().endswith("?")
        st["you"] += sum(t in SECOND for t in toks)
        st["words"] += len(toks)
    total = sum(v["seconds"] for v in stats.values()) or 1.0
    out = {}
    for sp, v in stats.items():
        q = v["questions"] / max(1, v["segments"])
        y = v["you"] / max(1, v["words"])
        share = v["seconds"] / total
        out[sp] = {"seconds": round(v["seconds"], 1), "talk_share": round(share, 3), "question_share": round(q, 3),
                   "second_person_share": round(y, 3), "segments": v["segments"],
                   "interviewer_score": round(0.5 * q + 4.0 * y + 0.3 * (1 - share), 3)}
    if len(out) == 1:
        sp = next(iter(out))
        out[sp].update(role="narrator", role_confidence=0.6, role_reason="single voice in the recording")
        return out
    # The guest is the dominant voice with the fewest questions; every other voice that clearly asks more is an
    # interviewer (press conferences and remote interviews often have several).
    guest = max(out, key=lambda k: out[k]["talk_share"] - out[k]["interviewer_score"])
    for k in out:
        out[k].update(role="unknown", role_confidence=0.0, role_reason="no clear question/answer pattern")
    gscore = out[guest]["interviewer_score"]
    asking = {k: out[k]["interviewer_score"] - gscore for k in out if k != guest}
    if asking and min(asking.values()) >= 0.15 and out[guest]["talk_share"] >= 0.4:
        out[guest].update(role="guest", role_confidence=round(min(0.9, 0.4 + min(asking.values())), 2),
                          role_reason=f"dominant voice ({out[guest]['talk_share']:.0%} of talk time) that rarely asks questions")
        for k, gap in asking.items():
            out[k].update(role="interviewer", role_confidence=round(min(0.95, 0.5 + gap), 2),
                          role_reason=f"asks questions ({out[k]['question_share']:.0%} of segments) and addresses the guest "
                                      f"('you' {out[k]['second_person_share']:.1%} of words)")
    return out


def diarize(path, analysis: dict, max_speakers: int = 4) -> dict:
    """Label transcript segments with speakers (in place on a copy) and return analysis['speakers']."""
    segs = ((analysis.get("transcript") or {}).get("segments")) or []
    if not segs:
        return {"available": False, "why": "no transcript segments to attribute"}
    x = _audio(path)
    neural = ensure_emb_model() is not None
    E, keep = embed_neural(x, segs) if neural else embed(x, segs)
    if len(E) < 4:
        return {"available": False, "why": "too little speech to compare voices"}
    lab, sil, k = _cluster(E, max_speakers)
    D = _cos_dist(np.vstack([E, np.array([E[lab == c].mean(0) for c in range(k)])]))[: len(E), len(E):]
    labels, conf = {}, {}
    for j, i in enumerate(keep):
        labels[i] = f"S{int(lab[j]) + 1}"
        d = np.sort(D[j])
        conf[i] = round(float((d[1] - d[0]) / (d[1] + 1e-9)) if k > 1 else 1.0, 3)
    for i in range(len(segs)):  # very short segments inherit the nearest labelled neighbour
        if i not in labels:
            near = min(labels, key=lambda j: abs(j - i))
            labels[i], conf[i] = labels[near], 0.0
    turns = []
    for i, s in enumerate(segs):
        sp = labels[i]
        if turns and turns[-1]["speaker"] == sp and float(s["start"]) - turns[-1]["end"] < 2.0:
            turns[-1]["end"] = float(s["end"])
            turns[-1]["conf"].append(conf[i])
        else:
            turns.append({"start": float(s["start"]), "end": float(s["end"]), "speaker": sp, "conf": [conf[i]]})
    for t in turns:
        t["confidence"] = round(float(np.mean(t.pop("conf"))), 3)
    return {"available": True, "method": ("WeSpeaker ResNet34 speaker embeddings (ONNX, CPU)" if neural else
                                          "MFCC statistics (numpy fallback)") + " + agglomerative clustering, speakers chosen by silhouette",
            "licence": EMB_LICENCE if neural else "own code (no model)", "n_speakers": k, "silhouette": round(sil, 3), "turns": turns,
            "segment_labels": [labels[i] for i in range(len(segs))], "speakers": roles(segs, labels),
            "note": "weaker than neural speaker embeddings; assumes one speaker per transcript segment"}


def apply_labels(analysis: dict, spk: dict) -> dict:
    """A copy of the analysis whose transcript segments carry `speaker` (role name when known, else S1/S2...)."""
    an = copy.deepcopy(analysis)
    an["speakers"] = spk
    if not spk.get("available"):
        return an
    names = {k: (v["role"] if v.get("role") in ("interviewer", "guest", "narrator") else k) for k, v in spk["speakers"].items()}
    for s, lab in zip(an["transcript"]["segments"], spk["segment_labels"]):
        s["speaker"] = names.get(lab, lab)
    return an


# ---------------------------------------------------------------- manual correction (the automatic result is kept)
def _editable(spk: dict) -> dict:
    out = copy.deepcopy(spk)
    out.setdefault("auto", {k: copy.deepcopy(spk[k]) for k in ("turns", "speakers", "segment_labels") if k in spk})
    out.setdefault("edits", [])
    return out


def rename(spk: dict, old: str, new_role: str, who: str = "user") -> dict:
    out = _editable(spk)
    out["speakers"][old]["role"], out["speakers"][old]["role_confidence"] = new_role, 1.0
    out["speakers"][old]["role_reason"] = f"set by {who}"
    out["edits"].append({"op": "rename", "speaker": old, "role": new_role, "by": who})
    return out


def set_name(spk: dict, label: str, name: str, who: str = "user") -> dict:
    """Give a detected speaker a display name (used for lower thirds). Roles alone are never shown on screen."""
    out = _editable(spk)
    out["speakers"][label]["name"] = name.strip()
    out["edits"].append({"op": "name", "speaker": label, "name": name.strip(), "by": who})
    return out


def merge(spk: dict, keep: str, drop: str, who: str = "user") -> dict:
    out = _editable(spk)
    out["segment_labels"] = [keep if x == drop else x for x in out["segment_labels"]]
    for t in out["turns"]:
        if t["speaker"] == drop:
            t["speaker"] = keep
    out["speakers"].pop(drop, None)
    out["n_speakers"] = len(out["speakers"])
    out["edits"].append({"op": "merge", "keep": keep, "drop": drop, "by": who})
    return out


def reassign_turn(spk: dict, index: int, speaker: str, who: str = "user") -> dict:
    out = _editable(spk)
    out["turns"][index]["speaker"], out["turns"][index]["confidence"] = speaker, 1.0
    out["edits"].append({"op": "reassign", "turn": index, "speaker": speaker, "by": who})
    return out
