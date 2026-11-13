"""Kaggle provider: batch GPU through the official Kaggle CLI (2.x) only.

    kaggle kernels push -p <dir> [--accelerator NvidiaTeslaT4] -t <seconds>   start a run ("Save & Run All")
    kaggle kernels status <owner>/<slug>                                      queued | running | complete | error
    kaggle kernels output <owner>/<slug> -p <dir> -o                          download /kaggle/working of the latest run
    kaggle config view                                                        account username (identity)

Policy, enforced here: exactly ONE Kaggle connection ("kaggle_primary"); kernels are private (is_private true); no
billable option is ever set; a quota/limit message pauses the provider (dispatcher fails over to a different provider
type or waits) - never another account. Cancellation: the CLI has no command that stops a running kernel version
(`kernels delete` deletes the whole notebook and is not used); a cancel stops polling, marks the job cancelled and starts
no further steps; the run ends by itself at the -t timeout we always pass.

The API token is read from the vault per call and passed as KAGGLE_API_TOKEN in the child environment only; every
piece of CLI output is passed through vault.redact() before it is stored, logged or returned.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from ... import costguard, vault
from ...contracts import ProviderHealth, QuotaStatus, RemoteWorkerCapability
from .. import worker_kit
from .base import Provider, ProviderError, QuotaExhausted, RemoteStatus, compute_dir, conn_state, save_conn

CONNECTION_ID = "kaggle_primary"   # the only Kaggle connection NEXUS will ever use
ACCELERATOR = "NvidiaTeslaT4"     # free "GPU T4 x2"

# Weekly GPU allowance. Kaggle shows the current quota on the account's settings page and describes it as varying
# ("floating"); it is not exposed by the CLI/API. Read 2026-11-12: kaggle.com/docs/efficient-gpu-usage body was not
# retrievable from this environment; the repo's zero-spend manifest (nexus/registry/components.yaml, kaggle-notebooks)
# records "historically ~30 h/week, 12 h max per session", which matches public summaries found the same day.
# NEXUS therefore uses 30 h as the DOCUMENTED default and lets the owner override it with the figure shown on their
# Kaggle settings page (settings.json "kaggle_gpu_hours_week"). Usage itself is MEASURED (our own accounting).
DOCUMENTED_GPU_HOURS_WEEK = 30.0
ALLOWANCE_SOURCE = ("documented default 30 h/week (Kaggle shows the floating current value on the account settings page; "
                    "not exposed by the CLI). Checked 2026-11-12; override with settings kaggle_gpu_hours_week")
SESSION_MAX_S = 12 * 3600

QUOTA_RE = re.compile(r"quota|exceeded|usage limit|limit (?:has been )?reached|too many (?:requests|sessions)|\b429\b|"
                      r"maximum (?:number of )?(?:concurrent|weekly|gpu)|no (?:more )?gpu", re.I)
AUTH_RE = re.compile(r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|could not find kaggle\.json|invalid token|authenticat", re.I)

Runner = Callable[[list, "Path | None", float], "tuple[int, str, str]"]


def _cli_runner(args: list, cwd: Path | None, timeout: float) -> tuple[int, str, str]:
    tok = vault.get(CONNECTION_ID)
    if not tok:
        raise ProviderError("Kaggle is not connected: no 'kaggle_primary' secret in the vault")
    env = {**os.environ, "KAGGLE_API_TOKEN": tok}
    try:
        r = subprocess.run(["kaggle", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"kaggle {args[0]} {args[1] if len(args) > 1 else ''} timed out after {timeout}s"
    return r.returncode, vault.redact(r.stdout), vault.redact(r.stderr)


def gpu_seconds_used(window_s: float = 7 * 86400, now: float | None = None) -> float:
    from ... import db
    now = now or time.time()
    rows = db.query("SELECT SUM(value) s FROM metrics WHERE name='kaggle_gpu_seconds' AND ts > ?", (now - window_s,))
    return float(rows[0]["s"] or 0.0)


def allowance_hours() -> float:
    from ... import config
    try:
        return float(config.settings().get("kaggle_gpu_hours_week") or DOCUMENTED_GPU_HOURS_WEEK)
    except (TypeError, ValueError):
        return DOCUMENTED_GPU_HOURS_WEEK


class KaggleProvider(Provider):
    provider = "kaggle"
    mode = "push"

    def __init__(self, connection_id: str = CONNECTION_ID, runner: Runner | None = None):
        if connection_id != CONNECTION_ID:
            raise ValueError("NEXUS uses exactly one Kaggle account (connection 'kaggle_primary'); account rotation is not allowed")
        super().__init__(connection_id)
        self.run = runner or _cli_runner

    # ---- identity / health
    def configured(self) -> tuple[bool, str]:
        if self.run is _cli_runner:
            if not shutil.which("kaggle"):
                return False, "the official kaggle CLI is not installed (pip install kaggle)"
            if not vault.get(CONNECTION_ID):
                return False, "no Kaggle token stored (vault secret 'kaggle_primary')"
        return True, ""

    def username(self, refresh: bool = False) -> str:
        st = conn_state(self.connection_id)
        if st.get("_identity") and not refresh:
            return st["_identity"]
        rc, out, err = self.run(["config", "view"], None, 60)
        m = re.search(r"username:\s*(\S+)", out)
        if rc != 0 or not m:
            raise ProviderError(f"could not read the Kaggle username: {(err or out)[-300:]}")
        save_conn(self.connection_id, "kaggle", identity=m.group(1))
        return m.group(1)

    def declared_capability(self) -> RemoteWorkerCapability:
        return RemoteWorkerCapability(provider="kaggle", connection_id=self.connection_id, accelerator="Tesla T4 x2 (declared, not measured)",
                                      vram_gb=None, max_runtime_s=SESSION_MAX_S, frameworks=["torch", "ffmpeg"], internet=True, measured=False)

    def quota(self) -> QuotaStatus:
        limit = allowance_hours() * 3600
        used = gpu_seconds_used()
        paused, _ = self.paused()
        return QuotaStatus(provider="kaggle", connection_id=self.connection_id, resource="gpu_seconds_week", limit=limit,
                           used=round(used, 1), remaining=round(max(0.0, limit - used), 1), period="weekly (rolling 7 days, conservative)",
                           source="measured", exhausted=paused or used >= limit)

    def health(self) -> ProviderHealth:
        ok, why = self.configured()
        st = conn_state(self.connection_id)
        q = self.quota()
        base = dict(provider="kaggle", connection_id=self.connection_id, identity=st.get("_identity"), quotas=[q],
                    last_success=st.get("_last_success"), last_error=st.get("_last_error"))
        if not ok:
            return ProviderHealth(status="not_configured", detail=why, **base)
        paused, reason = self.paused()
        if paused or q.exhausted:
            return ProviderHealth(status="quota_exhausted", detail=reason or "measured weekly GPU use reached the allowance; Kaggle work is paused", **base)
        if st.get("auth_failed"):
            return ProviderHealth(status="unauthorized", detail=st.get("auth_failed"), **base)
        return ProviderHealth(status="ok", detail=ALLOWANCE_SOURCE, **base)

    # ---- one attempt
    @staticmethod
    def slug(job_id: str) -> str:
        return "nexus-" + re.sub(r"[^a-z0-9]+", "-", job_id.lower()).strip("-")

    def _classify(self, rc: int, out: str, err: str, what: str) -> None:
        text = (out + "\n" + err).strip()
        if rc == 0 and not re.search(r"\berror\b|\bfailed\b", text, re.I):
            return
        if QUOTA_RE.search(text):
            raise QuotaExhausted(f"Kaggle {what}: {text[-400:]}")
        if AUTH_RE.search(text):
            save_conn(self.connection_id, "kaggle", status="unauthorized", auth_failed=text[-200:], last_error=text[-300:])
            raise ProviderError(f"Kaggle {what} unauthorised: {text[-300:]}")
        save_conn(self.connection_id, "kaggle", last_error=f"{what}: {text[-300:]}")
        raise ProviderError(f"Kaggle {what} failed (exit {rc}): {text[-400:]}")

    def submit(self, job_id: str, attempt: int, bundle: dict, gpu: bool, timeout_s: float) -> str:
        costguard.authorize("kaggle-notebooks", "submit")
        paused, reason = self.paused()
        if paused:
            raise QuotaExhausted(f"Kaggle is paused: {reason}")
        user = self.username()
        slug = self.slug(job_id)
        d = compute_dir("kaggle", slug)
        for p in d.iterdir():
            if p.is_file():
                p.unlink()
        staged = [i for i in bundle.get("inputs") or [] if i.get("dataset_file")]
        datasets = [self.stage_dataset(user, i) for i in staged]
        bundle = {**bundle, "inputs": [{k: v for k, v in i.items() if k != "_stage_path"} for i in bundle.get("inputs") or []]}
        (d / "nexus_job.py").write_text(worker_kit.build_script(bundle), encoding="utf-8")
        meta = {"id": f"{user}/{slug}", "title": slug, "code_file": "nexus_job.py", "language": "python", "kernel_type": "script",
                "is_private": True, "enable_gpu": bool(gpu), "enable_tpu": False, "enable_internet": True,
                "dataset_sources": datasets, "competition_sources": [], "kernel_sources": [], "model_sources": []}
        (d / "kernel-metadata.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        args = ["kernels", "push", "-p", str(d), "-t", str(int(min(timeout_s, SESSION_MAX_S)))]
        if gpu:
            args += ["--accelerator", ACCELERATOR]
        rc, out, err = self.run(args, None, 300)
        self._classify(rc, out, err, "push")
        save_conn(self.connection_id, "kaggle", status="ok", last_success=time.time(), auth_failed="")
        return f"{user}/{slug}"

    can_stage_inputs = True

    def stage_dataset(self, user: str, inp: dict, wait_s: float = 600) -> str:
        """Upload one large input as a PRIVATE Kaggle dataset (official `kaggle datasets create`, private unless --public)
        named after its content hash, so the same file is uploaded once and reused by later jobs. Returns its ref."""
        ref = f"{user}/nexus-in-{inp['sha256'][:16]}"
        rc, out, err = self.run(["datasets", "status", ref], None, 60)
        if rc == 0 and "ready" in (out + err).lower():
            return ref
        d = compute_dir("kaggle_datasets", ref.split("/")[1])
        for p in d.iterdir():
            if p.is_file():
                p.unlink()
        src = Path(inp["_stage_path"])
        try:
            os.link(src, d / inp["dataset_file"])
        except OSError:
            shutil.copyfile(src, d / inp["dataset_file"])
        (d / "dataset-metadata.json").write_text(json.dumps({"title": ref.split("/")[1], "id": ref,
                                                             "licenses": [{"name": "other"}]}), encoding="utf-8")
        rc, out, err = self.run(["datasets", "create", "-p", str(d), "-q"], None, 1800)
        if rc != 0 and "already" not in (out + err).lower():
            self._classify(rc, out, err, "dataset create")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            rc, out, err = self.run(["datasets", "status", ref], None, 60)
            text = (out + err).lower()
            if "ready" in text:
                return ref
            # a just-created private dataset answers 403/404 until it is visible (seen live): keep waiting
            if "error" in text and not re.search(r"\b(403|404)\b|not found|forbidden", text):
                raise ProviderError(f"Kaggle dataset {ref} failed to process: {(out + err)[-300:]}")
            time.sleep(10)
        raise ProviderError(f"Kaggle dataset {ref} not ready after {wait_s:.0f} s")

    def poll(self, ref: str) -> RemoteStatus:
        rc, out, err = self.run(["kernels", "status", ref], None, 60)
        text = (out + " " + err).strip()
        if rc != 0:
            if QUOTA_RE.search(text):
                raise QuotaExhausted(f"Kaggle status: {text[-300:]}")
            return RemoteStatus("unknown", text[-300:])
        low = text.lower()
        m = re.search(r'status\s+"?(?:kernelworkerstatus\.)?([a-z_]+)', low)
        word = m.group(1) if m else low
        for key, state in (("complete", "complete"), ("error", "error"), ("cancel", "cancelled"), ("running", "running"),
                           ("queued", "queued"), ("new_script", "queued")):
            if key in word:
                return RemoteStatus(state, text[-300:])
        return RemoteStatus("unknown", text[-300:])

    def fetch(self, ref: str, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        rc, out, err = self.run(["kernels", "output", ref, "-p", str(dest), "-o"], None, 900)
        self._classify(rc, out, err, "output")
        return dest

    def cancel(self, ref: str) -> str:
        return ("the official Kaggle CLI has no command to stop a running kernel version (only `kernels delete`, which deletes the "
                "notebook and is not used): NEXUS stopped polling, marked the job cancelled and starts no further steps; the "
                "run ends at its -t timeout")

    def log_tail(self, ref: str) -> str:
        d = compute_dir("kaggle", ref.split("/")[-1])
        logs = sorted(d.rglob("*.log"))
        if not logs:
            return ""
        raw = logs[-1].read_text(encoding="utf-8", errors="replace")
        try:  # Kaggle logs are a JSON array of {"stream_name","time","data"}
            raw = "".join(x.get("data", "") for x in json.loads(raw))
        except ValueError:
            pass
        return vault.redact(raw)[-4000:]
