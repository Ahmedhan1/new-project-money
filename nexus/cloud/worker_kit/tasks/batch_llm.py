# ruff: noqa: F821
"""batch_llm: bounded, temporary batch inference over a JSONL of prompts (Account 3 request A1-3).

One job = one model, one finite list of prompts, one run. The model is loaded inside the worker, every prompt is
answered, the outputs are written as verified artifacts and the process exits: there is NO serving endpoint, no open
port, no background process (Kaggle does not allow persistent serving). Routing (nexus/cloud/compute/tasks.py) restricts
the task to the authorised Kaggle GPU worker; the owner's own PC is never chosen, and real model engines refuse to
download weights anywhere but a Kaggle kernel unless NEXUS_BATCH_LLM_ALLOW_HERE=1 is set by the owner on that machine.

Input items (input JSONL artifact, one object per line, or params.items for small inline lists):
    {"id": "q1", "task": "summarization", "system": "...", "prompt": "...", "max_tokens": 256}
Output rows (batch_llm_part_NNNN.aK.jsonl per part; the control plane joins them into batch_llm_results.jsonl):
    {"id", "text", "usage": {"prompt_tokens", "completion_tokens"}, "finish_reason", "latency_s", "error"}

params:
    model             HF repo id (required), e.g. "Qwen/Qwen2.5-0.5B-Instruct"; model_revision optional (pins a commit)
    model_licence     recorded in provenance and the summary (default "unverified"; Account 3 checks commercial use)
    engine            "transformers" (default) | "vllm" (offline LLM.generate, only if already installed) | "echo"
                      ("echo" loads no model: deterministic test engine; its results are labelled simulated)
    output_format     "jsonl" (default) | "json"
    max_items         cap on items processed (default 500, hard cap 5000); extra items are refused, not truncated silently
    max_new_tokens    per-item cap (default 256, hard cap 4096); an item's own max_tokens is clipped to it
    max_prompt_chars  longer prompts are reported as item errors (default 16000)
    batch_size        prompts per generate call (default 8); part_size items per part/checkpoint (default 32)
    max_model_gb      refuse a model whose weight files exceed this (default 8.0); checked BEFORE downloading
    temperature       0 = greedy (default); seed (default 0)
    chat_template_kwargs  passed to apply_chat_template (e.g. {"enable_thinking": false} for Qwen3)
    time_budget_s     stop starting new parts after this (default 85 % of max_runtime_s); the job then resumes from
                      its last checkpoint in a new bounded attempt (counts against max_retries)
    simulate_loss_after_parts  tests only: on attempt 1, stop after that many parts
"""

BATCH_LLM_HARD_MAX_ITEMS = 5000
BATCH_LLM_HARD_MAX_TOKENS = 4096


class BatchLLMBudgetReached(SimulatedWorkerLoss):
    """Time budget reached with items left: reported as 'lost' so the dispatcher resumes from the last checkpoint."""


def _bl_items(ctx) -> list:
    if ctx.params.get("items") is not None:
        raw = list(ctx.params["items"])
    elif ctx.inputs:
        name = ctx.params.get("items_input")
        p = ctx.inputs[name] if name else next(iter(ctx.inputs.values()))
        raw = []
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    raw.append(json.loads(line))
                except ValueError as e:
                    raise ValueError(f"items JSONL line {n} is not JSON: {e}") from None
    else:
        raise ValueError("batch_llm needs params.items or an input JSONL artifact")
    cap = min(int(ctx.params.get("max_items", 500)), BATCH_LLM_HARD_MAX_ITEMS)
    if len(raw) > cap:
        raise ValueError(f"{len(raw)} items > max_items {cap}: split the batch into several jobs")
    seen = set()
    for it in raw:
        if not isinstance(it, dict) or not isinstance(it.get("id"), (str, int)) or not isinstance(it.get("prompt"), str):
            raise ValueError("every item needs an 'id' (str/int) and a 'prompt' (str)")
        if str(it["id"]) in seen:
            raise ValueError(f"duplicate item id {it['id']!r}")
        seen.add(str(it["id"]))
    return raw


def _bl_on_kaggle() -> bool:
    return Path("/kaggle/working").is_dir() or bool(os.environ.get("KAGGLE_KERNEL_RUN_TYPE"))


def _bl_model_bytes(model: str, revision) -> int | None:
    """Total size of the weight files, from the Hub's metadata (no download). None if it cannot be determined."""
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(model, revision=revision, files_metadata=True)
        exts = (".safetensors", ".bin", ".pt", ".gguf")
        return sum(int(s.size or 0) for s in info.siblings if s.rfilename.endswith(exts))
    except Exception:  # noqa: BLE001
        return None


class _BLEcho:
    """Deterministic engine for tests and dry-runs: loads nothing, answers 'echo[<task>]: <first words>'."""
    name, simulated = "echo", True

    def generate(self, batch: list, temperature: float, seed: int) -> list:
        out = []
        for it in batch:
            words = it["prompt"].split()
            n = max(1, min(len(words), it["_max_tokens"]))
            out.append({"text": f"echo[{it.get('task') or 'general'}]: " + " ".join(words[:n]),
                        "usage": {"prompt_tokens": len(words) + len((it.get("system") or "").split()), "completion_tokens": n},
                        "finish_reason": "stop" if n == len(words) else "length"})
        return out


class _BLTransformers:
    name, simulated = "transformers", False

    def __init__(self, model: str, revision, chat_kwargs: dict):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(model, revision=revision, padding_side="left")
        if self.tok.pad_token is None:
            self.tok.pad_token = self.tok.eos_token
        dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        self.model = AutoModelForCausalLM.from_pretrained(model, revision=revision, torch_dtype=dtype,
                                                          device_map="auto" if torch.cuda.is_available() else None)
        self.model.eval()
        self.chat_kwargs = chat_kwargs or {}

    def _text(self, it: dict) -> str:
        msgs = ([{"role": "system", "content": it["system"]}] if it.get("system") else []) + [{"role": "user", "content": it["prompt"]}]
        if getattr(self.tok, "chat_template", None):
            return self.tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, **self.chat_kwargs)
        return ((it.get("system") or "") + "\n\n" + it["prompt"]).strip()

    def generate(self, batch: list, temperature: float, seed: int) -> list:
        t = self.torch
        t.manual_seed(seed)
        enc = self.tok([self._text(it) for it in batch], return_tensors="pt", padding=True).to(self.model.device)
        mx = max(it["_max_tokens"] for it in batch)
        kw = {"max_new_tokens": mx, "pad_token_id": self.tok.pad_token_id}
        kw.update({"do_sample": True, "temperature": float(temperature)} if temperature > 0 else {"do_sample": False})
        with t.inference_mode():
            gen = self.model.generate(**enc, **kw)
        out, plen = [], enc["input_ids"].shape[1]
        for i, it in enumerate(batch):
            ids = gen[i][plen:].tolist()
            eos = self.tok.eos_token_id
            if eos in ids:
                ids, reason = ids[:ids.index(eos)], "stop"
            else:
                reason = "length"
            ids = ids[:it["_max_tokens"]]
            out.append({"text": self.tok.decode(ids, skip_special_tokens=True).strip(),
                        "usage": {"prompt_tokens": int(enc["attention_mask"][i].sum()), "completion_tokens": len(ids)},
                        "finish_reason": reason})
        return out


class _BLVllm:
    name, simulated = "vllm", False

    def __init__(self, model: str, revision, chat_kwargs: dict):
        from vllm import LLM  # only if the image already has it: this task never pip-installs an engine
        self.llm = LLM(model=model, revision=revision, dtype="half", enforce_eager=True)
        self.chat_kwargs = chat_kwargs or {}

    def generate(self, batch: list, temperature: float, seed: int) -> list:
        from vllm import SamplingParams
        convs = [([{"role": "system", "content": it["system"]}] if it.get("system") else []) + [{"role": "user", "content": it["prompt"]}]
                 for it in batch]
        outs = []
        for it, conv in zip(batch, convs):
            sp = SamplingParams(max_tokens=it["_max_tokens"], temperature=float(temperature), seed=seed)
            r = self.llm.chat([conv], sp, use_tqdm=False, chat_template_kwargs=self.chat_kwargs)[0]
            o = r.outputs[0]
            outs.append({"text": o.text.strip(), "usage": {"prompt_tokens": len(r.prompt_token_ids), "completion_tokens": len(o.token_ids)},
                         "finish_reason": o.finish_reason})
        return outs


def _bl_engine(ctx):
    engine = ctx.params.get("engine", "transformers")
    if engine == "echo":
        return _BLEcho()
    if engine not in ("transformers", "vllm"):
        raise ValueError(f"unknown engine {engine!r} (transformers | vllm | echo)")
    model = ctx.params.get("model")
    import re as _re
    if not model or not _re.fullmatch(r"[A-Za-z0-9][\w.-]*/[\w.-]+", str(model)):
        raise ValueError("params.model must be a Hugging Face repo id 'owner/name'")
    if not _bl_on_kaggle() and os.environ.get("NEXUS_BATCH_LLM_ALLOW_HERE") != "1":
        raise RuntimeError("refusing to download model weights outside a Kaggle kernel (owner machines are never used "
                           "implicitly; set NEXUS_BATCH_LLM_ALLOW_HERE=1 on a machine you chose for this)")
    cap_gb = float(ctx.params.get("max_model_gb", 8.0))
    size = _bl_model_bytes(model, ctx.params.get("model_revision"))
    ctx.metrics["model_weight_bytes"] = size
    if size is None:
        raise RuntimeError(f"cannot determine the weight size of {model} before download; refusing (max_model_gb={cap_gb})")
    if size > cap_gb * 2 ** 30:
        raise RuntimeError(f"{model} weights are {size / 2 ** 30:.2f} GiB > max_model_gb {cap_gb}")
    t0 = time.time()
    eng = (_BLVllm if engine == "vllm" else _BLTransformers)(model, ctx.params.get("model_revision"), ctx.params.get("chat_template_kwargs") or {})
    ctx.metrics["model_load_s"] = round(time.time() - t0, 2)
    ctx.metrics["model_download_bytes"] = size
    return eng


def _bl_generate(eng, batch: list, temperature: float, seed: int) -> list:
    """One batch; on an engine error (e.g. CUDA OOM) retry the items one by one, then record per-item errors."""
    t0 = time.time()
    try:
        res = eng.generate(batch, temperature, seed)
        dt = (time.time() - t0) / max(1, len(batch))
        return [{**r, "latency_s": round(dt, 4), "error": None} for r in res]
    except Exception as e:  # noqa: BLE001
        if len(batch) == 1:
            return [{"text": None, "usage": None, "finish_reason": "error", "latency_s": round(time.time() - t0, 4),
                     "error": f"{type(e).__name__}: {str(e)[:300]}"}]
        try:
            eng.torch.cuda.empty_cache()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return [r for it in batch for r in _bl_generate(eng, [it], temperature, seed)]


@task("batch_llm")
def task_batch_llm(ctx) -> dict:
    items = _bl_items(ctx)
    p = ctx.params
    tok_cap = min(int(p.get("max_new_tokens", 256)), BATCH_LLM_HARD_MAX_TOKENS)
    max_chars = int(p.get("max_prompt_chars", 16000))
    bs, part_size = max(1, int(p.get("batch_size", 8))), max(1, int(p.get("part_size", 32)))
    temperature, seed = float(p.get("temperature", 0.0)), int(p.get("seed", 0))
    fmt = p.get("output_format", "jsonl")
    if fmt not in ("jsonl", "json"):
        raise ValueError("output_format must be jsonl or json")
    budget = float(p.get("time_budget_s") or 0.85 * float(ctx.bundle.get("max_runtime_s") or 3600))
    parts = [items[i:i + part_size] for i in range(0, len(items), part_size)]
    state = {"parts_total": len(parts), "done": [], "part_files": {}, **ctx.resume_state}
    done = set(int(i) for i in state["done"])
    skipped = sorted(done)
    started = time.time()
    eng = None
    ran, n_ok, n_err, ptoks, ctoks, gen_s = [], 0, 0, 0, 0, 0.0
    stop_after = p.get("simulate_loss_after_parts")
    for pi, part in enumerate(parts):
        if pi in done:
            ctx.log(f"part {pi}: already done in an earlier attempt (checkpoint) - skipped")
            continue
        if ran and time.time() - started > budget:
            ctx.metrics.update(parts_run_this_attempt=ran, parts_skipped=skipped, items_ok=n_ok, items_error=n_err)
            raise BatchLLMBudgetReached(f"time budget {budget:.0f}s reached after {len(ran)} part(s); resume from checkpoint")
        if eng is None:
            eng = _bl_engine(ctx)
        rows, todo = [], []
        for it in part:
            row = {"id": it["id"], "task": it.get("task")}
            if len(it["prompt"]) + len(it.get("system") or "") > max_chars:
                rows.append({**row, "text": None, "usage": None, "finish_reason": "error", "latency_s": 0.0,
                             "error": f"prompt longer than max_prompt_chars {max_chars}"})
                continue
            todo.append({**it, "_max_tokens": max(1, min(int(it.get("max_tokens") or tok_cap), tok_cap))})
            rows.append(row)
        t0 = time.time()
        results = {}
        for b in range(0, len(todo), bs):
            batch = todo[b:b + bs]
            for it, r in zip(batch, _bl_generate(eng, batch, temperature, seed)):
                results[str(it["id"])] = r
        gen_s += time.time() - t0
        for row in rows:
            if "error" not in row:
                row.update(results[str(row["id"])])
            if row["error"]:
                n_err += 1
            else:
                n_ok += 1
                ptoks += row["usage"]["prompt_tokens"]
                ctoks += row["usage"]["completion_tokens"]
        name = f"batch_llm_part_{pi:04d}.a{ctx.attempt}.jsonl"
        f = ctx.work / name
        f.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        a = ctx.artifact(f, "analysis", part=pi, model=p.get("model"), engine=eng.name, simulated=eng.simulated)
        ran.append(pi)
        state["done"] = sorted(done | set(ran))
        state["part_files"][str(pi)] = {"name": a["name"], "sha256": a["sha256"], "items": len(rows), "attempt": ctx.attempt}
        ctx.checkpoint(f"part_{pi}", json.loads(json.dumps(state)))
        if stop_after is not None and ctx.attempt == 1 and len(ran) >= int(stop_after) and len(state["done"]) < len(parts):
            ctx.metrics.update(parts_run_this_attempt=ran, parts_skipped=skipped, items_ok=n_ok, items_error=n_err)
            raise SimulatedWorkerLoss(f"simulated worker loss after {len(ran)} part(s) (params.simulate_loss_after_parts)")
    summary = {"model": p.get("model"), "model_revision": p.get("model_revision"), "model_licence": p.get("model_licence", "unverified"),
               "engine": eng.name if eng else p.get("engine", "transformers"), "simulated": bool(eng.simulated) if eng else p.get("engine") == "echo",
               "items_total": len(items), "parts_total": len(parts), "part_files": state["part_files"], "output_format": fmt,
               "this_attempt": {"attempt": ctx.attempt, "parts_run": ran, "parts_skipped": skipped, "items_ok": n_ok,
                                "items_error": n_err, "prompt_tokens": ptoks, "completion_tokens": ctoks,
                                "generate_s": round(gen_s, 3),
                                "completion_tokens_per_s": round(ctoks / gen_s, 2) if gen_s > 0 else None},
               "limits": {"max_new_tokens": tok_cap, "max_prompt_chars": max_chars, "batch_size": bs, "part_size": part_size,
                          "time_budget_s": round(budget, 1), "max_model_gb": float(p.get("max_model_gb", 8.0))},
               "temperature": temperature, "seed": seed, "persistent_endpoint": False}
    ctx.write_json_artifact("batch_llm_summary.json", summary)
    return {"batch_llm": {k: summary[k] for k in ("model", "engine", "simulated", "items_total", "parts_total")},
            "parts_run_this_attempt": ran, "parts_skipped": skipped, "items_ok": n_ok, "items_error": n_err,
            "completion_tokens": ctoks, "completion_tokens_per_s": summary["this_attempt"]["completion_tokens_per_s"],
            "model_load_s": ctx.metrics.get("model_load_s")}
