"""Model benchmark V2: one fixed item set for every candidate, run where the model fits (local CPU or an authorised
remote GPU via Kaggle), scored HERE with the same deterministic scorers so results are comparable.

    python -m nexus.intelligence.model_router.bench_v2 kernel --models qwen3-4b,qwen3.5-9b --out DIR   # write a Kaggle job
    python -m nexus.intelligence.model_router.bench_v2 score RESULTS.json [...]                        # score raw outputs

Capabilities: reasoning, long_context, grounding (source synthesis, conflicts, unanswerable, grounded hooks),
instruction, json, summarization, translation (de, fr, ar, zh, ru, hi), dialect (Egyptian, Gulf), strategy, writing.
Scores are 0-1 per item; a model's capability score is the mean (x10 when stored for the router).
Nothing here trusts a model's own judgement: every check is a rule on the output text."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import bench as b1

SENSATIONAL = re.compile(r"\b(secret|shocking|nobody knows|you won'?t believe|mind[- ]blowing|insane|unbelievable|hidden truth)\b", re.I)

# ----------------------------------------------------------------- candidates (licences read from the HF model cards 2026-11-12)
MODELS: dict[str, dict] = {
    "qwen3-4b": {"pull": "qwen3:4b", "repo": "Qwen/Qwen3-4B", "licence": "Apache-2.0", "params_b": 4.0, "think": False},
    "qwen3-4b-think": {"pull": "qwen3:4b", "repo": "Qwen/Qwen3-4B", "licence": "Apache-2.0", "params_b": 4.0, "think": True},
    "qwen3.5-4b": {"pull": "hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", "repo": "Qwen/Qwen3.5-4B", "licence": "Apache-2.0", "params_b": 4.7, "think": False},
    "qwen3.5-9b": {"pull": "hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M", "repo": "Qwen/Qwen3.5-9B", "licence": "Apache-2.0", "params_b": 9.7, "think": False},
    "qwen3.6-35b-a3b": {"pull": "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:Q4_K_M", "repo": "Qwen/Qwen3.6-35B-A3B", "licence": "Apache-2.0", "params_b": 36.0,
                        "active_b": 3, "think": False},
    "qwen3.8-27b": {"pull": "hf.co/unsloth/Qwen3.8-27B-GGUF:Q4_K_M", "repo": "Qwen/Qwen3.8-27B", "licence": "Apache-2.0", "params_b": 27.8, "think": False},
    "deepseek-r1-0528-qwen3-8b": {"pull": "hf.co/unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF:Q4_K_M", "repo": "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
                                  "licence": "MIT", "params_b": 8.2, "think": True},
    "ministral-3-14b": {"pull": "hf.co/mistralai/Ministral-3-14B-Instruct-2512-GGUF:Q4_K_M", "repo": "mistralai/Ministral-3-14B-Instruct-2512",
                        "licence": "Apache-2.0", "params_b": 13.9, "think": False},
    "glm-4.7-flash": {"pull": "hf.co/unsloth/GLM-4.7-Flash-GGUF:Q4_K_M", "repo": "zai-org/GLM-4.7-Flash", "licence": "MIT", "params_b": 31.2, "think": False},
    "nemotron-nano-9b-v2": {"pull": "hf.co/bartowski/nvidia_NVIDIA-Nemotron-Nano-9B-v2-GGUF:Q4_K_M", "repo": "nvidia/NVIDIA-Nemotron-Nano-9B-v2",
                            "licence": "NVIDIA Open Model License (commercial use allowed under its terms)", "params_b": 8.9, "think": False},
}

# ----------------------------------------------------------------- items
REASON2 = [("A recipe needs 250 g of flour for 4 people. How many grams are needed for 10 people? Answer with the number.", "625"),
           ("Anna is older than Ben. Ben is older than Carl. Dana is younger than Carl. Who is the youngest?", "dana"),
           ("A car travels 150 km in 1 hour 40 minutes. What is its average speed in km/h? Answer with the number.", "90"),
           ("What is the largest possible number of Mondays in a 31-day month?", "5"),
           ("5 machines make 5 widgets in 5 minutes. How many minutes do 100 machines need to make 100 widgets?", "5"),
           ("A bat and a ball cost 1.10 euros in total. The bat costs 1.00 euro more than the ball. How many cents does the ball cost?", "5")]


def long_document() -> tuple[str, list[tuple[str, str]]]:
    """~2,500 words about a fictional town with distractor facts; answers need retrieval and one multi-hop step."""
    rivers = [("Orven", 88), ("Daltz", 143), ("Selm", 212), ("Kirra", 57), ("Mauve", 176)]
    paras = []
    for i in range(30):
        r, km = rivers[i % len(rivers)]
        paras.append(f"Section {i + 1}. The market of district {i + 3} sells wool, salt and copper on {['Mondays', 'Tuesdays', 'Fridays'][i % 3]}. "
                     f"Visitors often confuse the river {r}, which is {km + (i % 4) * 7 if r != 'Selm' else km} km long in local guides, with older maps. "
                     f"In {1800 + i * 7} a school for {['weavers', 'smiths', 'sailors', 'clerks'][i % 4]} opened near gate {i + 2}, and its registers "
                     f"list {120 + i * 13} pupils in the first year. The council later moved the archive twice, first to the mill and then to the tower. "
                     "Winters are long, and the harbour freezes for about six weeks in most years, which shapes trade and the calendar of festivals. ")
    paras[2] += "The town of Varenhal itself was founded in 1847 by salt traders from the coast. "
    paras[13] += "Official survey, 2020: the river Selm measures exactly 212 km from its spring to the sea. "
    paras[21] += "Ilse Marten served as mayor of Varenhal from 2016 until 2022; Tomas Rook has been mayor since 2022. "
    paras[26] += "The new Selm bridge was opened in 2021 after four years of construction. "
    qa = [("In which year was Varenhal founded?", "1847"),
          ("According to the official survey, how long is the river Selm in km?", "212"),
          ("Who was mayor of Varenhal when the new Selm bridge was opened?", "ilse marten")]
    return "\n\n".join(paras), qa


SOURCES = ('[S1] City press release (2019): The Narrow Gate footbridge opened on 4 May 2019. It is 48 m high and 310 m long.\n'
           '[S2] Engineering journal (2020): The footbridge, opened in May 2019, rises 48 m above the river and spans 310 m.\n'
           '[S3] Tourist blog (2023): The footbridge from 2019 is 52 m tall and a favourite spot for photographers.')
S_SYNTH = {"type": "object", "required": ["claims", "conflicts"], "properties": {
    "claims": {"type": "array", "items": {"type": "object", "required": ["text", "sources"], "properties": {
        "text": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}}}},
    "conflicts": {"type": "array", "items": {"type": "string"}}}}
S_STRAT = {"type": "object", "required": ["hook", "beats", "visuals"], "properties": {
    "hook": {"type": "string"}, "beats": {"type": "array", "items": {"type": "string"}}, "visuals": {"type": "array", "items": {"type": "string"}}}}
BRIEF = ("Topic: home batteries. Verified facts: lithium-ion battery pack prices fell about 90 percent between 2010 and 2023; "
         "a typical home battery stores 10 to 15 kWh; batteries can shift midday solar power to the evening peak.")


def taskset() -> list[dict]:
    items = [it for it in b1.taskset()]                       # V1 items kept unchanged: results stay comparable with V1
    items += [{"id": f"reason2_{i}", "cap": "reasoning", "schema": b1.S_ANSWER, "max_tokens": 80, "gold": g,
               "prompt": f'{q}\nReply as JSON: {{"answer": "<answer>"}}'} for i, (q, g) in enumerate(REASON2, 1)]
    doc, qa = long_document()
    items += [{"id": f"longctx{i}", "cap": "long_context", "schema": b1.S_ANSWER, "max_tokens": 60, "gold": g, "num_ctx": 8192,
               "prompt": f'Read the document and answer from it only.\n--- DOCUMENT ---\n{doc}\n--- END ---\nQuestion: {q}\nReply as JSON: {{"answer": "<answer>"}}'}
              for i, (q, g) in enumerate(qa, 1)]
    items += [
        {"id": "synth", "cap": "grounding", "schema": S_SYNTH, "max_tokens": 400,
         "prompt": f"Sources:\n{SOURCES}\nList the facts these sources support, each with the ids of every source that states it, and list "
                   'every disagreement between sources. Reply as JSON {"claims": [{"text": .., "sources": ["S1", ..]}], "conflicts": [..]}.'},
        {"id": "unanswerable", "cap": "grounding", "schema": b1.S_ANSWER, "max_tokens": 80,
         "prompt": f'Sources:\n{SOURCES}\nAccording to the sources only, how much did the footbridge cost? If the sources do not say, answer '
                   '"not stated". Reply as JSON {"answer": "<answer>"}.'},
        {"id": "grounded_hook", "cap": "grounding", "max_tokens": 80,
         "prompt": f"Sources:\n{SOURCES}\nWrite ONE opening sentence (max 25 words) for a short video about this footbridge. Use only facts "
                   "that at least two sources agree on. No exaggeration. Output only the sentence."},
        {"id": "instr_list", "cap": "instruction", "max_tokens": 80,
         "prompt": "List exactly 3 benefits of walking. Each line must start with '- ', have at most 8 words, and must not contain the word 'very'. Output only the list."},
        {"id": "instr_german", "cap": "instruction", "max_tokens": 120,
         "prompt": "Answer in German with exactly two sentences: Why is sleep important? Output only the two sentences."},
        {"id": "instr_count", "cap": "instruction", "schema": {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}},
         "max_tokens": 40, "prompt": 'How many vowels (a, e, i, o, u) does the word "education" contain? Reply with JSON {"count": <number>}.'},
        {"id": "translate_ar-EG", "cap": "dialect", "want": "egy", "max_tokens": 120,
         "prompt": f"Translate into colloquial Egyptian Arabic (not Modern Standard Arabic). Output only the translation.\nText: {b1.TRANSLATE_SRC}"},
        {"id": "translate_ar-Gulf", "cap": "dialect", "want": "gulf", "max_tokens": 120,
         "prompt": f"Translate into colloquial Gulf Arabic (Emirati/Saudi, not Modern Standard Arabic). Output only the translation.\nText: {b1.TRANSLATE_SRC}"},
        {"id": "strategy", "cap": "strategy", "schema": S_STRAT, "max_tokens": 350,
         "prompt": f"{BRIEF}\nPlan a 60-second explainer. Return JSON with: hook (one sentence, max 25 words, a question or a fact from the "
                   "brief, no exaggeration), beats (exactly 3 short story beats), visuals (exactly 3 concrete shots). Use only the verified facts."},
    ]
    for it in items:                                          # V1 dialect generation counts under 'dialect' too
        if it["id"].startswith("dialect_"):
            it["cap"] = "dialect"
    return items


# ----------------------------------------------------------------- scoring
def _answer(text: str) -> str:
    d = b1.parse_json(text)
    return str(d.get("answer", "")).strip().lower() if isinstance(d, dict) else (text or "").strip().lower()


def _numbers(text: str) -> set[str]:
    return {n.rstrip(".,") for n in re.findall(r"\d[\d.,]*", text or "")}


def score_v2(it: dict, text: str, back: str | None = None) -> dict:
    i = it["id"]
    if i.startswith("reason2_") or i.startswith("longctx"):
        a, g = _answer(text), it["gold"]
        ok = g in a if not g.isdigit() else bool(re.search(rf"(?<![\d.]){re.escape(g)}(?![\d])", a))
        return {"score": 1.0 if ok else 0.0}
    if i == "synth":
        d = b1.parse_json(text) or {}
        claims = d.get("claims") or [] if isinstance(d, dict) else []
        conf = " ".join(map(str, d.get("conflicts") or [])) if isinstance(d, dict) else ""
        multi = [c for c in claims if isinstance(c, dict) and len(set(c.get("sources") or [])) >= 2]
        year_ok = any("2019" in str(c.get("text")) and len(set(c.get("sources") or [])) >= 2 for c in claims if isinstance(c, dict))
        conflict_ok = "48" in conf and "52" in conf
        invented = _numbers(json.dumps(d, ensure_ascii=False)) - _numbers(SOURCES) - {"1", "2", "3"}
        parts = [bool(claims), bool(multi), year_ok, conflict_ok, not invented]
        return {"score": round(sum(parts) / len(parts), 3), "invented_numbers": sorted(invented)}
    if i == "unanswerable":
        a = _answer(text)
        return {"score": 1.0 if (("not" in a or "unknown" in a or "no " in a) and not re.search(r"\d", a)) else 0.0}
    if i == "grounded_hook":
        t = (text or "").strip()
        bad_num = _numbers(t) - _numbers(SOURCES)
        parts = [0 < len(t.split()) <= 25, not SENSATIONAL.search(t), not bad_num, "52" not in t]
        return {"score": round(sum(parts) / len(parts), 3)}
    if i == "instr_list":
        lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
        parts = [len(lines) == 3, all(ln.startswith("- ") for ln in lines), all(len(ln[2:].split()) <= 8 for ln in lines),
                 "very" not in (text or "").lower()]
        return {"score": round(sum(parts) / len(parts), 3)}
    if i == "instr_german":
        t = (text or "").strip()
        sents = [s for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
        de = len(re.findall(r"\b(der|die|das|ist|und|nicht|wir|der|weil|Schlaf|wichtig)\b", t, re.I)) >= 2
        return {"score": round(((len(sents) == 2) + de) / 2, 3)}
    if i == "instr_count":
        d = b1.parse_json(text)
        return {"score": 1.0 if isinstance(d, dict) and str(d.get("count")) == "5" else 0.0}
    if i.startswith("translate_ar-"):
        r = b1.score_dialect(text, it["want"])
        own = b1.script_share(text, "ar")
        return {**r, "score": round(0.7 * r["score"] + 0.3 * own, 3)}
    if i == "strategy":
        d = b1.parse_json(text) or {}
        if not isinstance(d, dict):
            return {"score": 0.0}
        hook = str(d.get("hook", ""))
        beats, vis = d.get("beats") or [], d.get("visuals") or []
        allowed = _numbers(BRIEF) | {"60", "1", "2", "3"}
        invented = _numbers(json.dumps(d, ensure_ascii=False)) - allowed
        parts = [0 < len(hook.split()) <= 25, ("?" in hook) or bool(_numbers(hook) & _numbers(BRIEF)), not SENSATIONAL.search(json.dumps(d)),
                 len(beats) == 3, len(vis) == 3 and all(len(str(v).split()) >= 3 for v in vis), not invented]
        return {"score": round(sum(parts) / len(parts), 3), "invented_numbers": sorted(invented)}
    return b1.score_item(it, text, back)


# ----------------------------------------------------------------- remote job (Kaggle notebook running Ollama)
KERNEL_TEMPLATE = r'''
import json, os, subprocess, time, urllib.request
OUT = "/kaggle/working/bench_results.json"
ITEMS = json.loads(__ITEMS__)
MODELS = json.loads(__MODELS__)
def sh(c, t=1800):
    r = subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t); return r.returncode, (r.stdout + r.stderr)[-2000:]
log = {"started": time.time(), "env": {}, "models": {}}
log["env"]["nvidia_smi"] = sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader")[1]
def save(): json.dump(log, open(OUT, "w"))
log["env"]["internet"] = sh("curl -sI https://huggingface.co | head -1")[1].strip()
# Ollama release tarball straight from GitHub (no systemd/sudo needed); install script as fallback
steps = ["mkdir -p /tmp/ollama && curl -fsSL -o /tmp/ollama.tgz https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tgz"
         " && tar -xzf /tmp/ollama.tgz -C /tmp/ollama && ln -sf /tmp/ollama/bin/ollama /usr/local/bin/ollama",
         "apt-get install -y -qq zstd >/dev/null 2>&1; curl -fsSL https://ollama.com/install.sh | sh"]
log["env"]["install"] = []
for c in steps:
    rc, o = sh(c, 1500); log["env"]["install"].append({"rc": rc, "out": o[-500:]}); save()
    if sh("ollama --version")[0] == 0: break
srv = subprocess.Popen(["/usr/local/bin/ollama", "serve"], stdout=open("/kaggle/working/ollama.log", "w"), stderr=subprocess.STDOUT,
                       env={**os.environ, "OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_NUM_PARALLEL": "1"})
for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2); break
    except Exception: time.sleep(2)
log["env"]["ollama_version"] = sh("ollama --version")[1].strip()
def chat(model, it, think):
    body = {"model": model, "stream": False, "messages": [{"role": "system", "content": "You are a precise assistant. Follow the format exactly."},
            {"role": "user", "content": it["prompt"]}], "options": {"temperature": 0, "num_ctx": it.get("num_ctx", 4096),
            "num_predict": (it["max_tokens"] + 3000) if think else it["max_tokens"]}, "think": bool(think)}
    if it.get("schema"): body["format"] = it["schema"]
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.time(); d = json.loads(urllib.request.urlopen(req, timeout=900).read())
    return d, time.time() - t0
for name, m in MODELS.items():
    rec = {"spec": m, "items": []}; t0 = time.time()
    rc, out = sh(f"ollama pull {m['pull']}", 3600); rec["pull_s"] = round(time.time() - t0, 1); rec["pull_ok"] = rc == 0; rec["pull_log"] = out[-400:]
    if rc != 0:
        log["models"][name] = rec; json.dump(log, open(OUT, "w")); continue
    try: chat(m["pull"], {"prompt": "hi", "max_tokens": 5}, False)
    except Exception as e: rec["warmup_error"] = str(e)[:300]
    rec["vram"] = sh("nvidia-smi --query-gpu=memory.used --format=csv,noheader")[1].strip()
    rec["ps"] = sh("ollama ps")[1][-500:]
    for it in ITEMS:
        r = {"id": it["id"]}
        try:
            d, lat = chat(m["pull"], it, m.get("think"))
            r.update(text=d["message"].get("content", ""), thinking_chars=len(d["message"].get("thinking") or ""), latency_s=round(lat, 2),
                     eval_count=d.get("eval_count"), eval_s=round((d.get("eval_duration") or 0) / 1e9, 2),
                     prompt_tokens=d.get("prompt_eval_count"))
        except Exception as e:
            r["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        rec["items"].append(r)
        log["models"][name] = rec; json.dump(log, open(OUT, "w"))
    rec["total_s"] = round(time.time() - t0, 1)
    sh(f"ollama stop {m['pull']}"); log["models"][name] = rec; json.dump(log, open(OUT, "w"))
log["finished"] = time.time(); json.dump(log, open(OUT, "w"))
'''


def kernel_source(model_ids: list[str]) -> str:
    items = [{k: v for k, v in it.items() if k in ("id", "prompt", "schema", "max_tokens", "num_ctx")} for it in taskset()]
    models = {m: MODELS[m] for m in model_ids}
    return (KERNEL_TEMPLATE.replace("__ITEMS__", repr(json.dumps(items, ensure_ascii=False)))
            .replace("__MODELS__", repr(json.dumps(models))))


def account1_manifest(job_id: str, model_ids: list[str]) -> dict:
    """The same job expressed as Account 1's ProductionJobManifest/v1 (task 'batch_llm'), recorded with the run so the
    dispatcher can take it over once Account 1's branch is merged."""
    return {"contract": "ProductionJobManifest/v1", "job_id": job_id, "task": "batch_llm", "owner_account": "account3",
            "params": {"models": model_ids, "items": len(taskset()), "runtime": "ollama"}, "requires": {"gpu": True, "vram_gb": 15},
            "priority": 5, "max_runtime_s": 12 * 3600, "preferred_providers": ["kaggle"], "idempotency_key": job_id}


def score_results(raw: dict, back_fn=None) -> dict:
    """raw = bench_results.json from the notebook. back_fn(lines, lang) -> back-translations (local MADLAD) or None."""
    from collections import defaultdict
    items = {it["id"]: it for it in taskset()}
    out = {}
    for name, rec in raw["models"].items():
        rows = []
        for r in rec.get("items", []):
            it = items[r["id"]]
            text = b1._strip_think(r.get("text") or "") if hasattr(b1, "_strip_think") else re.sub(r"<think>.*?</think>", "", r.get("text") or "", flags=re.S)
            back = None
            if it["id"].startswith("translate_") and it.get("lang") and back_fn and not r.get("error"):
                back = (back_fn([text], it["lang"]) or [None])[0]
            s = {"score": 0.0} if r.get("error") else score_v2(it, text, back)
            tps = (r["eval_count"] / r["eval_s"]) if r.get("eval_count") and r.get("eval_s") else None
            rows.append({"id": r["id"], "cap": it["cap"], **s, "latency_s": r.get("latency_s"), "tokens_per_s": round(tps, 1) if tps else None,
                         "thinking_chars": r.get("thinking_chars"), "error": r.get("error"), "text": text[:300]})
        caps = defaultdict(list)
        for x in rows:
            caps[x["cap"]].append(x["score"])
        lat = [x["latency_s"] for x in rows if x.get("latency_s")]
        tps = [x["tokens_per_s"] for x in rows if x.get("tokens_per_s")]
        out[name] = {"spec": rec["spec"], "pull_ok": rec.get("pull_ok"), "pull_s": rec.get("pull_s"), "vram": rec.get("vram"),
                     "capabilities": {c: round(sum(v) / len(v), 3) for c, v in caps.items()}, "n": {c: len(v) for c, v in caps.items()},
                     "overall": round(sum(x["score"] for x in rows) / max(1, len(rows)), 3), "failures": sum(1 for x in rows if x.get("error")),
                     "mean_latency_s": round(sum(lat) / len(lat), 2) if lat else None,
                     "median_tokens_per_s": sorted(tps)[len(tps) // 2] if tps else None, "rows": rows}
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="bench_v2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("kernel")
    k.add_argument("--models", required=True)
    k.add_argument("--out", required=True)
    k.add_argument("--slug", required=True)
    k.add_argument("--user", default="ahmedhanimohamed")
    a = ap.parse_args(argv)
    if a.cmd == "kernel":
        ids = a.models.split(",")
        d = Path(a.out)
        d.mkdir(parents=True, exist_ok=True)
        (d / "bench.py").write_text(kernel_source(ids), encoding="utf-8")
        (d / "kernel-metadata.json").write_text(json.dumps({"id": f"{a.user}/{a.slug}", "title": a.slug, "code_file": "bench.py",
                                                            "language": "python", "kernel_type": "script", "is_private": True,
                                                            "enable_gpu": True, "enable_tpu": False, "enable_internet": True,
                                                            "dataset_sources": [], "competition_sources": [], "kernel_sources": [],
                                                            "model_sources": []}, indent=1), encoding="utf-8")
        (d / "account1_manifest.json").write_text(json.dumps(account1_manifest(a.slug, ids), indent=1), encoding="utf-8")
        print(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
