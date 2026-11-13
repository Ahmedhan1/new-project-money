"""Heavy text on the owner's Kaggle GPU (nexus/remote_llm.py): routing, connect, progress notes, failure messages.

Kaggle is the fake CLI from test_cloud_compute (it runs the pushed worker script locally), with the worker's "echo"
engine, so the real dispatcher, worker bundle, checkpoints, verification and result join all run. No network, no model.
"""
import json

import pytest
from test_cloud_compute import FAKE_TOKEN, FakeKaggleCLI

from nexus import config, db, jobs, llm, remote_llm, vault
from nexus.cloud.compute import dispatcher as dsp
from nexus.cloud.compute.kaggle import KaggleProvider, token_env
from nexus.cloud.compute.local import LocalProvider


@pytest.fixture()
def kaggle(home, tmp_path, monkeypatch):
    fake = FakeKaggleCLI(tmp_path / "kaggle_remote")
    d = dsp.Dispatcher({"kaggle": KaggleProvider(runner=fake), "local": LocalProvider()}, poll_min=0, poll_max=0)
    dsp.set_dispatcher(d)
    monkeypatch.setattr(remote_llm, "ENGINE", "echo")
    monkeypatch.setattr(remote_llm.time, "sleep", lambda s: None)
    yield fake
    dsp.set_dispatcher(None)


def _settings(**kv):
    (config.HOME / "settings.json").write_text(json.dumps(kv), encoding="utf-8")


def test_chat_goes_to_kaggle_and_never_loads_the_local_model(kaggle, monkeypatch):
    monkeypatch.setattr(llm, "load", lambda: pytest.fail("the local 2.5 GB model must not be loaded when Kaggle is used"))
    out = llm.chat("Say hello about volcanoes", "You are terse.", max_tokens=64, temperature=0.3)
    assert out  # echo engine answers with text derived from the prompt
    push = next(c for c in kaggle.calls if c[:2] == ["kernels", "push"])
    assert "--accelerator" in push                    # a GPU run, on the one authorised Kaggle connection
    assert kaggle.last_meta["is_private"] is True


def test_progress_is_shown_on_the_running_nexus_job(kaggle):
    seen = []

    @jobs.handler("kaggle-text")
    def h(job):
        orig = job.note

        def spy(msg, progress=None):
            seen.append(msg)
            orig(msg, progress)
        job.note = spy
        return job.stage("script", lambda: llm.chat("Write one line", "sys", 32, 0.2))

    jobs.submit("kaggle-text")
    assert jobs.run_job(jobs.claim("w1")) == "done"
    assert any(m.startswith("Kaggle GPU: job sent") for m in seen)
    assert seen[-1] == "Kaggle GPU: answer received"


def test_schema_is_written_into_the_prompt(kaggle):
    schema = {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}}
    remote_llm.chat("Give a title", "sys", 64, 0.2, schema=schema)
    row = db.query("SELECT manifest FROM remote_jobs ORDER BY created DESC LIMIT 1")[0]
    prompt = json.loads(row["manifest"])["params"]["items"][0]["prompt"]
    assert "JSON Schema" in prompt and '"required": ["title"]' in prompt


def test_mode_local_keeps_the_cpu_path(kaggle, monkeypatch):
    _settings(heavy_compute="local")
    assert remote_llm.use_kaggle() is False


def test_mode_kaggle_without_connection_fails_clearly_instead_of_downloading(home, monkeypatch):
    dsp.set_dispatcher(dsp.Dispatcher({"kaggle": KaggleProvider(), "local": LocalProvider()}))
    try:
        _settings(heavy_compute="kaggle")
        monkeypatch.setattr(llm, "load", lambda: pytest.fail("must not fall back to the local model"))
        with pytest.raises(remote_llm.KaggleNotReady, match="System > Kaggle"):
            llm.chat("x", "y", 10, 0.1)
        _settings(heavy_compute="auto")
        assert remote_llm.use_kaggle() is False   # auto without Kaggle: the local path, as before
    finally:
        dsp.set_dispatcher(None)


def test_connect_stores_the_token_in_the_vault_and_reads_the_username(kaggle, monkeypatch):
    monkeypatch.setattr("nexus.cloud.compute.kaggle.cli_command", lambda: ["kaggle"])
    r = remote_llm.connect(FAKE_TOKEN)
    assert r == {"connected": True, "username": "tester"}
    assert vault.get("kaggle_primary") == FAKE_TOKEN
    st = remote_llm.status()
    assert st["ready"] and st["username"] == "tester" and st["mode"] == "auto"


def test_connect_rejects_junk_and_bad_kaggle_json(home):
    with pytest.raises(ValueError, match="Kaggle API token"):
        remote_llm.connect("abc")
    with pytest.raises(ValueError, match="username"):
        remote_llm.connect('{"user": "x", "secret": "0123456789abcdef"}')


def test_legacy_kaggle_json_becomes_username_and_key_env():
    assert token_env('{"username": "me", "key": "k" }') == {"KAGGLE_USERNAME": "me", "KAGGLE_KEY": "k"}
    assert token_env(FAKE_TOKEN) == {"KAGGLE_API_TOKEN": FAKE_TOKEN}


def test_api_mode_and_status(kaggle):
    from fastapi.testclient import TestClient

    from nexus import api
    c = TestClient(api.app)
    assert c.post("/api/kaggle/mode", json={"mode": "nope"}).status_code == 400
    assert c.post("/api/kaggle/mode", json={"mode": "kaggle"}).json()["mode"] == "kaggle"
    assert json.loads((config.HOME / "settings.json").read_text())["heavy_compute"] == "kaggle"
    assert c.get("/api/kaggle").json()["ready"] is True
    assert c.post("/api/kaggle/connect", json={"token": "short"}).status_code == 400
