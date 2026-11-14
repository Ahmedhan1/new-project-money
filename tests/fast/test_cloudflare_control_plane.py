"""Cloudflare control plane (cloudflare/): the Kaggle worker it pushes must be the current worker_kit, and the hosting
component must stay zero-spend. JS behaviour is tested by `npm test` in cloudflare/ (node --test)."""
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_generated_worker_script_is_current():
    r = subprocess.run([sys.executable, str(ROOT / "scripts/cloudflare/build_worker_script.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_cloudflare_component_is_free_and_declared():
    comps = {c["id"]: c for c in yaml.safe_load((ROOT / "nexus/registry/components.yaml").read_text(encoding="utf-8"))["components"]}
    cf = comps["cloudflare-workers"]
    assert cf["payment_method_required"] is False
    assert "api.kaggle.com" in comps["kaggle-notebooks"]["network_hosts"]


def test_no_secret_values_in_wrangler_config():
    cfg = (ROOT / "cloudflare/wrangler.jsonc").read_text(encoding="utf-8")
    assert "KGAT_" not in cfg and "OWNER_PASSWORD=" not in cfg
    assert not (ROOT / "cloudflare/.dev.vars").exists() or ".dev.vars" in (ROOT / "cloudflare/.gitignore").read_text()
