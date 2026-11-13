"""Drive the real app in Chromium: Asset Library (install, preview, favourite), Cinematic Workspace Assets card (apply to
clip and to timeline), Storage page (disk dashboard, cleanup dry run, archive browser). Writes a JSON result and one
screenshot per page into the output directory.

    python scripts/acceptance/ui_assets_storage_check.py --out docs/results/a2_assets
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "results" / "a2_assets"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="nexus-ui-"))
    env = {**os.environ, "NEXUS_HOME": str(home), "NEXUS_SCHEDULER_THREAD": "0"}
    # a project with a two-clip timeline (made through the library code, as the editor would)
    setup = ("import subprocess,sys; sys.path.insert(0,sys.argv[1]); from nexus import config; config.ensure_dirs(); "
             "from nexus import project as prj; from nexus.timeline import ops; p=prj.Project.create('UI check'); "
             "import tempfile,pathlib; tmpf=str(pathlib.Path(tempfile.mkdtemp())/'s.mp4'); subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=s=320x180:r=30:d=4',"
             "'-f','lavfi','-i','sine=d=4','-shortest','-c:v','libx264','-preset','ultrafast','-c:a','aac',tmpf],check=True); "
             "src=p.import_media(tmpf)['path']; tl=prj.new_timeline(320,180); "
             "tl['clips']=[ops.video_clip(src,0,2),ops.video_clip(src,2,4)]; p.save_timeline(tl,'start'); print(p.id)")
    pid = subprocess.run([sys.executable, "-c", setup, str(ROOT)], env=env, capture_output=True, text=True, check=True).stdout.strip()
    port = free_port()
    srv = subprocess.Popen([sys.executable, "-m", "nexus", "serve", "--port", str(port)], cwd=str(ROOT), env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = subprocess.Popen([sys.executable, "-m", "nexus", "worker"], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    res: dict = {"date": time.strftime("%Y-%m-%d"), "steps": []}

    def step(name, ok, detail=""):
        res["steps"].append({"step": name, "ok": bool(ok), "detail": str(detail)[:300]})
        print(("PASS " if ok else "FAIL ") + name, detail)

    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            exe = os.environ.get("NEXUS_CHROMIUM") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
            b = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1400, "height": 1000})
            errors: list[str] = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(base + "/#assets")
            pg.click("nav a[data-id=assets]")
            pg.wait_for_selector("#lc", timeout=30000)
            n = pg.locator(".card button[data-fav]").count()
            step("asset library lists the catalog", n >= 170, f"{n} asset cards")
            pg.select_option("#lc", "luts")
            pg.wait_for_selector("[data-inst='lut.teal_orange']", timeout=30000)
            pg.click("[data-inst='lut.teal_orange']")
            pg.wait_for_selector("[data-un='lut.teal_orange']", timeout=60000)
            img = pg.locator("img[src*='lut.teal_orange']")
            img.wait_for(timeout=60000)
            pg.wait_for_function("i => i.complete && i.naturalWidth > 0", arg=img.element_handle(), timeout=60000)
            step("install a LUT from the browser and see its rendered preview", True, "lut.teal_orange installed, preview loaded")
            pg.click("[data-fav='lut.teal_orange']")
            pg.wait_for_selector("[data-fav='lut.teal_orange'][data-on='0']", timeout=20000)
            step("mark a favourite", True)
            pg.select_option("#lc", "transitions")
            heart = pg.locator("img[src*='transition.shape_heart']")
            heart.wait_for(state="attached", timeout=30000)
            heart.scroll_into_view_if_needed()
            pg.wait_for_function("i => i.complete && i.naturalWidth > 0", arg=heart.element_handle(), timeout=90000)
            step("transition previews render on demand", True, "transition.shape_heart preview loaded")
            pg.screenshot(path=str(out / "ui_asset_library.jpg"), quality=60, type="jpeg")
            pg.click("[id=lver]")
            pg.wait_for_selector("#lmsg .note", timeout=30000)
            step("verify checksums from the browser", "match their SHA-256" in pg.inner_text("#lmsg"), pg.inner_text("#lmsg"))
            # workspace
            pg.goto(base + "/")
            pg.evaluate(f"go('workspace','{pid}')")
            pg.wait_for_selector("#wal [data-aa]", timeout=30000)
            pg.click(".tl div[data-c]")
            pg.wait_for_selector("#wal [data-t=clip]", timeout=30000)
            pg.click("#wal [data-aa='effect.vhs'][data-t=clip]")
            pg.wait_for_selector("#wal [data-aa]", timeout=30000)
            tl = json.loads(urllib.request.urlopen(f"{base}/api/projects/{pid}").read())["timeline"]
            step("apply an effect to the selected clip in the workspace", any(e.get("type") == "vhs" for e in tl["clips"][0].get("effects", [])),
                 tl["clips"][0].get("effects"))
            pg.select_option("#wac", "transitions")
            pg.wait_for_selector("#wal [data-aa='transition.film_burn'][data-t=timeline]", timeout=30000)
            pg.click("#wal [data-aa='transition.film_burn'][data-t=timeline]")
            pg.wait_for_selector("#wal [data-aa]", timeout=30000)
            time.sleep(1)
            tl = json.loads(urllib.request.urlopen(f"{base}/api/projects/{pid}").read())["timeline"]
            step("apply a transition to the whole timeline", tl["clips"][1].get("xfade") == "film_burn", tl["clips"][1].get("xfade"))
            pg.screenshot(path=str(out / "ui_workspace_assets.jpg"), quality=60, type="jpeg")
            # storage page
            pg.evaluate("go('storage')")
            pg.wait_for_selector("#dsp", timeout=30000)
            step("disk dashboard shows free space and categories", "free on" in pg.inner_text("#dsk") and "Render cache" in pg.inner_text("#dsk"))
            pg.click("#dsp")
            pg.wait_for_selector("#dsplan p", timeout=30000)
            step("cleanup dry run lists what can be removed and what is protected", "protected" in pg.inner_text("#dsplan"), pg.inner_text("#dsplan")[:200])
            pg.wait_for_selector("#arc p", timeout=30000)
            step("archive browser shows the destination state", "Destination" in pg.inner_text("#arc"), pg.inner_text("#arc")[:200])
            pg.screenshot(path=str(out / "ui_storage.jpg"), quality=60, type="jpeg", full_page=True)
            step("no JavaScript errors", not errors, errors[:3])
            b.close()
    except Exception as e:  # noqa: BLE001
        step("exception", False, f"{type(e).__name__}: {e}")
    finally:
        srv.terminate()
        worker.terminate()
    res["ok"] = all(s["ok"] for s in res["steps"])
    (out / "ui_check.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
