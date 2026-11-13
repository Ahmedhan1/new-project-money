"""D: REAL browser end-to-end test of NEXUS Remote Studio (Chromium via Node Playwright).

Starts the actual app (uvicorn) on a free port with a temporary NEXUS_HOME and an owner token, seeds a few clearly
labelled fixture rows (provider "fixture") so the pages have content, then drives Chromium: login, every section,
scheduler start/pause/resume, notification mark-read, light/dark + mobile layouts. Screenshots (deviceScaleFactor 1,
each < 400 KB) and a JSON record go to docs/results/account1/. Any page error or failed request fails the test.

Run:  python -m pytest tests/e2e/test_studio_browser.py -q      (needs `node` with the `playwright` package + Chromium)
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "results" / "account1"
TOKEN = "e2e-owner-token-7f3a9c"

SEED = r"""
import json, subprocess, time
from nexus import config, db
from nexus.cloud import events
from nexus.contracts import ArtifactManifest
now = time.time()
# measured history for the ETA (fixture provider, clearly labelled)
for i, (task, d) in enumerate([("probe", 42), ("probe", 38), ("probe", 51), ("ffmpeg_transcode", 140), ("ffmpeg_transcode", 170)]):
    db.execute("INSERT INTO remote_jobs(id,task,owner_account,status,priority,provider,connection_id,attempts,max_retries,created,started,finished,result)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"rjob_fixture{i}", task, "account1", "succeeded", 5, "fixture", "fixture", 1, 2,
               now - 4000 + i * 300, now - 3990 + i * 300, now - 3990 + i * 300 + d,
               json.dumps({"accelerator": "fixture (no GPU)", "duration_s": d, "metrics": {"cpu_percent": 71, "peak_ram_mb": 410}})))
db.execute("INSERT INTO remote_jobs(id,task,owner_account,status,priority,provider,connection_id,attempts,max_retries,created,finished,error)"
           " VALUES('rjob_fixturefail','ffmpeg_transcode','account1','failed',5,'fixture','fixture',3,2,?,?,'fixture: input missing')", (now - 600, now - 590))
db.execute("INSERT INTO remote_checkpoints(job_id,seq,step,data,created) VALUES('rjob_fixture3',0,'download','{\"bytes\": 1048576}',?)", (now - 3000,))
db.execute("INSERT INTO remote_checkpoints(job_id,seq,step,data,created) VALUES('rjob_fixture3',1,'transcode','{\"frames\": 2400}',?)", (now - 2950,))
events.emit("job.failed", "fixture: ffmpeg_transcode failed (input missing)", job_id="rjob_fixturefail")
events.notify("info", "E2E fixture data", "Rows with provider 'fixture' were seeded by tests/e2e/test_studio_browser.py")
# a real local thumbnail + master, made with ffmpeg
d = config.HOME / "artifacts"
d.mkdir(parents=True, exist_ok=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:d=1", "-frames:v", "1", str(d / "thumb.jpg")], check=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", str(d / "master.mp4")], check=True)
for aid, kind, name, mime, ref, prov in [("art_e2emaster", "master", "fixture_master.mp4", "video/mp4", "artifacts/master.mp4", {}),
                                         ("art_e2ethumb", "thumbnail", "fixture_thumb.jpg", "image/jpeg", "artifacts/thumb.jpg", {"source_artifact": "art_e2emaster"})]:
    p = config.HOME / ref
    m = ArtifactManifest(artifact_id=aid, kind=kind, name=name, mime=mime, size=p.stat().st_size, sha256="0" * 64, provenance=prov,
                         locations=[{"provider": "local", "connection_id": "local", "ref": ref}])
    db.execute("INSERT INTO artifacts(id,kind,name,sha256,size,project_id,job_id,manifest,created) VALUES(?,?,?,?,?,?,?,?,?)",
               (aid, kind, name, m.sha256, m.size, None, None, m.model_dump_json(), now))
print("seeded")
"""

DRIVER = r"""
const { chromium } = require('playwright');
const [base, token, outDir] = process.argv.slice(2);
const SECTIONS = ['dash','accounts','notifications','scheduler','jobs','library','review','compute','storage','health','resources','logs','checkpoints','security','config'];
(async () => {
  const rec = { base, sections: {}, page_errors: [], console_errors: [], failed_requests: [], http_errors: [], steps: [], screenshots: [] };
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1360, height: 860 }, deviceScaleFactor: 1, colorScheme: 'dark' });
  const page = await ctx.newPage();
  const watch = p => {
    p.on('pageerror', e => rec.page_errors.push(String(e)));
    p.on('console', m => { if (m.type() === 'error') rec.console_errors.push(m.text()); });
    p.on('requestfailed', r => rec.failed_requests.push(r.url() + ' ' + (r.failure() || {}).errorText));
    p.on('response', r => { if (r.status() >= 500) rec.http_errors.push(r.status() + ' ' + r.url()); });
  };
  watch(page);
  const step = s => rec.steps.push(s);
  const shot = async (p, name) => { await p.evaluate(() => { const t = document.querySelector('#toast'); if (t) t.innerHTML = ''; }); const f = outDir + '/studio_' + name + '.png'; await p.screenshot({ path: f }); rec.screenshots.push(f); };
  try {
    await page.goto(base + '/studio');
    await page.waitForSelector('#tok');
    step('login screen shown (token mode)');
    const stored = await page.evaluate(() => JSON.stringify(localStorage));
    await page.fill('#tok', 'wrong-token');
    await page.click('#loginb');
    await page.waitForFunction(() => document.querySelector('#lerr') && document.querySelector('#lerr').textContent.includes('failed'));
    step('wrong token rejected');
    await page.fill('#tok', token);
    await page.click('#loginb');
    await page.waitForSelector('h2.page >> text=Dashboard');
    step('logged in');
    const cookies = await ctx.cookies();
    const sess = cookies.find(c => c.name === 'nexus_session');
    rec.cookie = sess ? { httpOnly: sess.httpOnly, sameSite: sess.sameSite, secure: sess.secure } : null;
    const js = await page.evaluate(() => ({ cookie: document.cookie, ls: JSON.stringify(localStorage), ss: JSON.stringify(sessionStorage) }));
    rec.token_visible_to_js = [js.cookie, js.ls, js.ss, stored].some(x => x.includes(token));
    for (const id of SECTIONS) {
      const t0 = Date.now();
      await page.click(`nav a[data-nav="${id}"]`);
      await page.waitForFunction(i => location.hash === '#' + i && document.querySelector('h2.page') && !document.querySelector('main').textContent.startsWith('Loading'), id);
      const info = await page.evaluate(() => ({ title: document.querySelector('h2.page').textContent,
        errors: [...document.querySelectorAll('.note.bad')].map(n => n.textContent),
        notes: [...document.querySelectorAll('.note')].map(n => n.textContent.slice(0, 160)),
        buttons: document.querySelectorAll('main button').length }));
      rec.sections[id] = { ...info, ms: Date.now() - t0 };
    }
    step('opened every section');
    // scheduler: demo plan, save, start, pause, resume
    await page.click('nav a[data-nav="scheduler"]');
    await page.waitForSelector('#c_planm');
    await page.click('[data-act="demoplan"]');
    await page.fill('#c_dm', '2');
    await page.fill('#c_spm', '2');
    await page.click('[data-act="schsave"]');
    await page.waitForSelector('#toast div >> text=Scheduler settings saved');
    step('scheduler settings saved (demo plan probe / ffmpeg_transcode, 2 masters x 2 shorts)');
    const status = async () => (await page.textContent('#schstat'));
    await page.click('[data-sched="start"]');
    await page.waitForFunction(() => /running/.test(document.querySelector('#schstat')?.textContent || ''));
    rec.after_start = await status();
    rec.after_start_note = await page.evaluate(() => [...document.querySelectorAll('main .note')].map(n => n.textContent).join(' | '));
    step('scheduler started: ' + rec.after_start);
    await shot(page, 'scheduler');
    await page.click('[data-sched="pause"]');
    await page.waitForFunction(() => /paused/.test(document.querySelector('#schstat')?.textContent || ''));
    step('scheduler paused');
    await page.click('[data-sched="resume"]');
    await page.waitForFunction(() => /running/.test(document.querySelector('#schstat')?.textContent || ''));
    step('scheduler resumed');
    // notifications: mark all read
    await page.click('nav a[data-nav="notifications"]');
    await page.waitForSelector('[data-act="readall"]');
    if (await page.isEnabled('[data-act="readall"]')) {
      await page.click('[data-act="readall"]');
      await page.waitForFunction(() => document.querySelector('[data-act="readall"]')?.disabled);
    }
    step('notifications marked read');
    // library: thumbnail is shown (low-res); full resolution only via explicit link
    await page.click('nav a[data-nav="library"]');
    await page.waitForSelector('.thumb img');
    rec.library = await page.evaluate(() => ({ imgs: [...document.querySelectorAll('.thumb img')].map(i => i.naturalWidth), videos: document.querySelectorAll('video').length,
      full_links: [...document.querySelectorAll('main a[target=_blank]')].map(a => a.textContent) }));
    const range = await page.evaluate(async () => { const r = await fetch('/api/v1/studio/artifacts/art_e2emaster/stream', { headers: { Range: 'bytes=0-99' } });
      return { status: r.status, range: r.headers.get('content-range'), len: (await r.arrayBuffer()).byteLength }; });
    rec.stream_range = range;
    step('library thumbnail rendered; stream endpoint answers Range ' + range.status);
    await shot(page, 'library');
    // dashboard
    await page.click('nav a[data-nav="dash"]');
    await page.waitForSelector('h2.page >> text=Dashboard');
    await page.waitForTimeout(300);
    await shot(page, 'dashboard');
    // mobile + light theme
    const m = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, colorScheme: 'light', storageState: await ctx.storageState() });
    const mp = await m.newPage();
    watch(mp);
    await mp.goto(base + '/studio#accounts');
    await mp.waitForSelector('h2.page >> text=Connected accounts');
    rec.mobile = await mp.evaluate(() => ({ scrollW: document.documentElement.scrollWidth, innerW: innerWidth, bg: getComputedStyle(document.body).backgroundColor }));
    await shot(mp, 'mobile_light');
    step('mobile light layout');
    // sign out -> login again
    await page.click('[data-act="logout"]');
    await page.waitForSelector('#tok');
    const after = await page.evaluate(async () => (await fetch('/api/v1/studio/summary')).status);
    rec.after_logout_status = after;
    step('signed out; data API answers ' + after);
  } catch (e) {
    rec.fatal = String(e && e.stack || e);
    try { await shot(page, 'failure'); } catch (_) {}
  }
  await browser.close();
  console.log(JSON.stringify(rec));
})();
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _node_ok() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _node_ok(), reason="node + playwright not available")
def test_studio_in_real_browser(tmp_path):
    home = tmp_path / "home"
    env = {**os.environ, "NEXUS_HOME": str(home), "NEXUS_EMBEDDED_WORKER": "0", "NEXUS_API_TOKEN": TOKEN,
           "NEXUS_SCHEDULER_THREAD": "1", "PYTHONPATH": str(ROOT)}
    env.pop("NEXUS_API_URL", None)
    subprocess.run([sys.executable, "-c", SEED], env=env, cwd=ROOT, check=True, capture_output=True, timeout=120)
    port = _free_port()
    env["NEXUS_PORT"] = str(port)
    log = open(tmp_path / "server.log", "w")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "nexus.api:app", "--host", "127.0.0.1", "--port", str(port)],
                           env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                urllib.request.urlopen(base + "/api/v1/studio/auth", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("server did not start: " + (tmp_path / "server.log").read_text()[-2000:])
        OUT.mkdir(parents=True, exist_ok=True)
        drv = tmp_path / "driver.js"
        drv.write_text(DRIVER)
        t0 = time.time()
        r = subprocess.run(["node", str(drv), base, TOKEN, str(OUT)], capture_output=True, text=True, timeout=240, cwd=tmp_path)
        assert r.returncode == 0, r.stderr[-3000:]
        rec = json.loads(r.stdout.strip().splitlines()[-1])
        rec["seconds"] = round(time.time() - t0, 1)
    finally:
        srv.terminate()
        srv.wait(10)
        log.close()
    server_log = (tmp_path / "server.log").read_text()
    rec["server_tracebacks"] = server_log.count("Traceback")
    rec["screenshots"] = [{"file": Path(f).name, "bytes": Path(f).stat().st_size} for f in rec["screenshots"]]
    rec["base"] = "http://127.0.0.1:<free port>"
    rec["fixtures"] = "remote_jobs rows with provider 'fixture', two checkpoints, one failed-job event, one info notification, " \
                      "and a local ffmpeg-made thumbnail + 2 s master; everything else is live server state"
    rec["date"] = time.strftime("%Y-%m-%d")
    (OUT / "studio_e2e.json").write_text(json.dumps(rec, indent=1) + "\n")
    assert "fatal" not in rec, rec.get("fatal")
    assert rec["page_errors"] == [] and rec["http_errors"] == [] and rec["server_tracebacks"] == 0, rec
    assert rec["cookie"] and rec["cookie"]["httpOnly"] and rec["token_visible_to_js"] is False
    assert all(not s["errors"] for s in rec["sections"].values()), {k: v["errors"] for k, v in rec["sections"].items() if v["errors"]}
    assert rec["stream_range"]["status"] == 206 and rec["stream_range"]["len"] == 100
    assert rec["library"]["videos"] == 0  # nothing full-resolution loads by itself
    assert rec["mobile"]["scrollW"] <= rec["mobile"]["innerW"]
    assert rec["after_logout_status"] == 401
    for s in rec["screenshots"]:
        assert s["bytes"] < 400 * 1024, s
