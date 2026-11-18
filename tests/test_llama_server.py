import dataclasses
import json
import os
import sys
import time

import pytest
from starlette.testclient import TestClient

from vault_ingest import llama_server, models
from vault_ingest.app import Engine
from vault_ingest.config import PROJECT_DIR, load_settings
from vault_ingest.gpu import GpuStateStore, ModelManager, make_manager
from vault_ingest.llama_server import LlamaServerManager, offloaded, setup_check
from vault_ingest.llm import LLMUnavailable, LocalStudioLLM, make_llm
from vault_ingest.server import make_app
from vault_ingest.watcher_meetings import load_config

# A stand-in for llama-server: the flags the manager passes, /health (503 while "loading"), /v1/models,
# and a streamed chat answer that needs the key from LLAMA_API_KEY.
STUB = r'''#!PYTHON
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
args = sys.argv[1:]
opt = lambda name: args[args.index(name) + 1]
port, alias, key = int(opt("--port")), opt("--alias"), os.environ.get("LLAMA_API_KEY", "")
open(os.environ.get("STUB_ARGS", os.devnull), "w").write(json.dumps(args))
if os.environ.get("STUB_FAIL"):
    print("error: failed to load model"); sys.exit(1)
print("load_tensors: offloaded 31/33 layers to GPU", flush=True)
ready = time.time() + 0.4
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok"}) if time.time() > ready else self.reply(503, {"error": "Loading model"})
        if self.path == "/v1/models":
            return self.reply(200, {"data": [{"id": alias}]})
        self.reply(404, {"error": "not found"})
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Authorization") != f"Bearer {key}":
            return self.reply(401, {"error": "Invalid API Key"})
        answer = json.dumps({"context": "A kickoff about the spring campaign.", "points": ["Budget 12000 EUR", "Deadline 15 March", "Review terms"]})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for piece in (answer[:20], answer[20:]):
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n".encode())
        self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': {'completion_tokens': 42}})}\n\ndata: [DONE]\n\n".encode())
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


@pytest.fixture
def home(tmp_path):
    """A data folder with a downloaded server build (the stub) and a downloaded model."""
    cat = models.load_catalogue()
    root = tmp_path / "home"
    build = models.entry(cat, "llama-linux-cpu")
    exe_dir = models.folder(build, root, cat) / "llama-b12380"
    exe_dir.mkdir(parents=True)
    exe = exe_dir / "llama-server"
    exe.write_text(STUB.replace("PYTHON", sys.executable), encoding="utf-8")
    exe.chmod(0o755)
    (exe_dir.parent / ".complete").write_text(json.dumps({"server": "llama-b12380/llama-server"}), encoding="utf-8")
    chat = models.entry(cat, "gemma-4-e4b")
    folder = models.folder(chat, root, cat)
    folder.mkdir(parents=True)
    for f in chat["files"]:
        (folder / f["name"]).write_bytes(b"gguf")
    (folder / ".complete").write_text("{}", encoding="utf-8")
    return root


@pytest.fixture
def builtin(settings):
    return dataclasses.replace(settings, llm_mode="local", llm_provider="openai_compat", local_engine="builtin",
                               llm_model="gemma-4-e4b")


def manager(settings, home, **kw):
    return LlamaServerManager(settings, root=home, backend="cpu", nvidia=lambda args: None, **kw)


@pytest.mark.skipif(os.name == "nt", reason="the stub is a shebang script")
def test_the_server_starts_with_the_catalogue_model_answers_and_stops(builtin, home, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_ARGS", str(tmp_path / "args.json"))
    m = manager(builtin, home)
    try:
        assert m.ensure_loaded() == "gemma-4-e4b"
        args = json.loads((tmp_path / "args.json").read_text())
        model_dir = home / "models" / "gemma-4-e4b"
        assert args[:4] == ["-m", str(model_dir / "gemma-4-E4B-it-Q4_K_M.gguf"), "--mmproj", str(model_dir / "mmproj-F16.gguf")]
        for flag, value in (("--host", "127.0.0.1"), ("--alias", "gemma-4-e4b"), ("-ngl", "auto"), ("-np", "1"), ("-c", "8192")):
            assert args[args.index(flag) + 1] == value
        assert "--api-key" not in args                           # the key goes in the environment
        info = json.loads((home / "state" / "llama-server.json").read_text())
        assert builtin.openai_compat_base_url == f"http://127.0.0.1:{info['port']}/v1"
        assert m.list_models() == [{"id": "gemma-4-e4b", "type": "llm", "state": "loaded"}] and m.ours_loaded()
        llm = make_llm(builtin)                                   # the existing client, unchanged in use
        assert isinstance(llm, LocalStudioLLM) and not llm.auto_start
        from vault_ingest.llm import BENCH_SYSTEM, BENCH_USER
        from vault_ingest.schemas import ChunkDigest
        assert llm.complete_json(BENCH_SYSTEM, BENCH_USER, ChunkDigest).points[0] == "Budget 12000 EUR"
        assert llm.resolve_model() == "gemma-4-e4b"
        assert m.offload() == (31, 33)
    finally:
        m.unload_chat()
    assert not (home / "state" / "llama-server.json").exists()
    assert not llama_server._is_llama(info["pid"])
    assert m.list_models()[0]["state"] == "not-loaded"


@pytest.mark.skipif(os.name == "nt", reason="the stub is a shebang script")
def test_the_other_engine_process_uses_the_same_server_and_a_pause_there_stops_it(builtin, home):
    watch = manager(builtin, home)
    serve_settings = dataclasses.replace(builtin)
    serve = manager(serve_settings, home)
    try:
        watch.ensure_loaded()
        pid = json.loads((home / "state" / "llama-server.json").read_text())["pid"]
        assert serve.ours_loaded() and serve.managed_loaded_ids() == ["gemma-4-e4b"]
        serve.unload_chat()                                       # what GpuService does when it pauses
        assert not llama_server._is_llama(pid)
        GpuStateStore(builtin.system / "gpu-state.json").claim_pause(reason="a game", manual=False, until=None)
        from vault_ingest.gpu import GpuInterrupted
        with pytest.raises(GpuInterrupted):
            watch.ensure_loaded()                                 # paused: nothing is started again
    finally:
        watch.unload_chat()


@pytest.mark.skipif(os.name == "nt", reason="the stub is a shebang script")
def test_a_server_that_fails_to_load_reports_its_log(builtin, home, monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "1")
    with pytest.raises(LLMUnavailable, match="failed to load model"):
        manager(builtin, home).ensure_loaded()
    assert not (home / "state" / "llama-server.json").exists()


def test_nothing_downloaded_says_where_to_get_it(builtin, tmp_path):
    with pytest.raises(LLMUnavailable, match="Settings > AI engine"):
        manager(builtin, tmp_path / "empty").ensure_loaded()


def test_a_dead_pid_in_the_state_file_is_not_a_server(builtin, home):
    (home / "state").mkdir()
    (home / "state" / "llama-server.json").write_text(json.dumps({"pid": os.getpid(), "port": 9, "key": "k"}))
    m = manager(builtin, home)
    assert m.info() is None                                       # the pid is this test, not llama-server
    assert m.list_models()[0]["state"] == "not-loaded"


def test_the_servers_own_vram_is_not_counted_as_another_app(builtin, home):
    (home / "state").mkdir()
    rows = "4242, 6900\n555, 300\n"
    m = manager(builtin, home)
    m._nvidia = lambda args: rows
    assert m.other_vram_mb() == 7200
    m.info = lambda: {"pid": 4242, "port": 1, "key": "k"}
    assert m.other_vram_mb() == 300


@pytest.mark.skipif(os.name == "nt", reason="the stub is a shebang script")
def test_test_my_setup_reports_speed_and_fit_then_unloads(builtin, home):
    m = manager(builtin, home)
    m._nvidia = lambda args: "7100, 8192\n" if "--query-gpu=memory.used,memory.total" in args else ""
    result = setup_check(builtin, manager=m)
    assert result["ok"], result
    assert result["model"] == "gemma-4-e4b" and result["tokens_per_sec"] > 0
    assert result["offloaded"] == (31, 33) and result["fits"] is False and result["vram"] == [7100, 8192]
    assert m.info() is None                                       # unloaded again


def test_test_my_setup_reports_a_failure_without_raising(builtin, tmp_path):
    result = setup_check(builtin, manager=manager(builtin, tmp_path / "empty"))
    assert not result["ok"] and "not downloaded" in result["message"]


def test_offload_line():
    assert offloaded("x\nload_tensors: offloaded 49/49 layers to GPU\n") == (49, 49)
    assert offloaded("load_tensors: offloaded 10/49 layers to GPU\nload_tensors: offloaded 20/49 layers to GPU") == (20, 49)
    assert offloaded("CPU only") is None


# ------------------------------------------------------------------ which engine
def test_the_engine_setting_picks_the_manager(settings):
    lm = dataclasses.replace(settings, llm_mode="local", local_engine="lmstudio")
    assert type(make_manager(lm)) is ModelManager and make_manager(lm).managing
    other = dataclasses.replace(settings, llm_mode="local", local_engine="server")
    assert not make_manager(other).managing                       # Ollama: nothing is started or stopped
    assert isinstance(make_manager(dataclasses.replace(lm, local_engine="builtin")), LlamaServerManager)
    assert make_llm(lm).auto_start                                # LM Studio may still be started with `lms`


def test_env_keys(tmp_path, vault, monkeypatch):
    for key in ("LLM_MODE", "LOCAL_ENGINE", "LOCAL_MODEL", "BUILTIN_MODEL"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"VAULT_PATH={vault}\nLLM_MODE=local\nLOCAL_MODEL=google/gemma-3-12b\n", encoding="utf-8")
    s = load_settings(env)
    assert s.local_engine == "lmstudio" and s.llm_model == "google/gemma-3-12b"     # an old .env keeps working
    monkeypatch.setenv("LOCAL_ENGINE", "builtin")
    monkeypatch.setenv("BUILTIN_MODEL", "gemma-4-12b")
    assert load_settings(env).llm_model == "gemma-4-12b"
    monkeypatch.setenv("LOCAL_ENGINE", "lms")
    with pytest.raises(SystemExit, match="LOCAL_ENGINE"):
        load_settings(env)


def test_the_llm_reads_address_and_key_on_each_request(settings):
    s = dataclasses.replace(settings, llm_mode="local", openai_compat_base_url="http://127.0.0.1:1/v1")
    llm = LocalStudioLLM(s, auto_start=False)
    s.openai_compat_base_url, s.openai_compat_api_key = "http://127.0.0.1:2/v1", "new"
    assert llm.base == "http://127.0.0.1:2/v1" and llm.root == "http://127.0.0.1:2"
    import httpx
    assert llm._auth(httpx.Request("GET", "http://x")).headers["Authorization"] == "Bearer new"
    llm.model = "m"
    assert "ttl" in llm._body("s", "u", None, None)              # LM Studio unloads by itself after the ttl
    own = LocalStudioLLM(dataclasses.replace(s, local_engine="builtin", llm_model="m"), auto_start=False)
    assert "ttl" not in own._body("s", "u", None, None)


# ------------------------------------------------------------------ the API
TOKEN = "t"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def engine(vault, settings, tmp_path, monkeypatch):
    for key in ("LLM_MODE", "LOCAL_ENGINE", "LOCAL_BASE_URL", "BUILTIN_MODEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"VAULT_PATH={vault}\n", encoding="utf-8")
    cfg = dataclasses.replace(load_config(PROJECT_DIR / "companion.toml"), screenshot_folder=vault / "nope")
    e = Engine(settings, cfg, env_file=env, data_dir=tmp_path / "home")
    e._hardware = models.hardware(e.catalogue, nvidia_text="NVIDIA GeForce RTX 3070, 8192", system="Windows", ram_mb=32768)
    yield e
    e.downloads.stop()
    for key in ("LLM_MODE", "LOCAL_ENGINE", "LOCAL_BASE_URL", "BUILTIN_MODEL", "ANTHROPIC_API_KEY"):
        os.environ.pop(key, None)


def test_ai_settings_show_this_computers_downloads(engine):
    client = TestClient(make_app(engine, TOKEN), base_url="http://127.0.0.1")
    assert client.get("/settings/ai").status_code == 401
    data = client.get("/settings/ai", headers=AUTH).json()
    assert data["engine"] == "cloud" and data["model"] == "gemma-4-12b" and data["hardware"]["vram_mb"] == 8192
    assert [i["id"] for i in data["items"]] == ["llama-win-cuda", "gemma-4-12b", "gemma-4-e4b", "whisper-large-v3",
                                                "cuda-libs-win", "llama-win-cpu", "bge-m3"]
    assert [i["id"] for i in data["items"] if i["optional"]] == ["llama-win-cpu", "bge-m3"]
    first = data["items"][0]
    assert first["state"] == "missing" and first["size"] == 319405686 + 391443627 and first["checked"] == "sha256"
    assert data["items"][1]["checked"] == "size" and data["free_bytes"] > 0


def test_choosing_an_engine_writes_env_and_reloads(engine, vault):
    client = TestClient(make_app(engine, TOKEN), base_url="http://127.0.0.1")
    r = client.patch("/settings/ai", headers=AUTH, json={"engine": "builtin", "model": "gemma-4-e4b"})
    assert r.status_code == 200 and r.json()["engine"] == "builtin" and r.json()["model"] == "gemma-4-e4b"
    text = engine.env_file.read_text()
    assert "LLM_MODE=local" in text and "LOCAL_ENGINE=builtin" in text and "BUILTIN_MODEL=gemma-4-e4b" in text
    assert engine.s.local_engine == "builtin" and engine.s.llm_model == "gemma-4-e4b"
    r = client.patch("/settings/ai", headers=AUTH, json={"engine": "ollama", "address": "http://localhost:11434/"})
    assert r.json()["engine"] == "ollama" and r.json()["address"] == "http://localhost:11434"
    assert engine.s.openai_compat_base_url == "http://localhost:11434/v1" and "LOCAL_ENGINE=server" in engine.env_file.read_text()
    r = client.patch("/settings/ai", headers=AUTH, json={"engine": "cloud"})
    assert r.status_code == 400 and "key" in r.json()["error"]
    r = client.patch("/settings/ai", headers=AUTH, json={"engine": "cloud", "api_key": "sk-ant-test-1234567890"})
    assert r.json()["engine"] == "cloud" and r.json()["key_hint"] == "...7890"
    assert "1234567890" not in json.dumps(client.get("/settings/ai", headers=AUTH).json())   # the key is never sent back
    for bad in ({"engine": "gpt"}, {"engine": "builtin", "model": "llama-win-cpu"},
                {"engine": "lmstudio", "address": "localhost; rm -rf"}, {"engine": "cloud", "api_key": "a b\nc"}):
        assert client.patch("/settings/ai", headers=AUTH, json=bad).status_code == 400


def test_a_change_waits_while_a_meeting_is_being_written(engine, vault):
    (vault / "_System" / "ingest-status.json").write_text(json.dumps({"file": "x.txt", "step": "summary"}))
    engine.change_ai({"engine": "lmstudio"})
    assert engine._ai_pending and engine.s.effective_mode != "local"
    (vault / "_System" / "ingest-status.json").write_text(json.dumps({"file": "", "step": ""}))
    assert engine._apply_ai() and not engine._ai_pending and engine.s.local_engine == "lmstudio"


def test_downloads_start_only_when_asked_and_unknown_ones_are_404(engine):
    client = TestClient(make_app(engine, TOKEN), base_url="http://127.0.0.1")
    assert client.post("/settings/ai/downloads/nope", headers=AUTH, json={}).status_code == 404
    assert client.post("/settings/ai/downloads/gemma-4-12b", headers=AUTH, json={"action": "x"}).status_code == 400
    assert engine.downloads.jobs == {}
    started = []
    engine.downloads.start = lambda i: started.append(i) or {"id": i, "state": "queued"}
    assert client.post("/settings/ai/downloads/gemma-4-12b", headers=AUTH, json={}).json()["state"] == "queued"
    assert started == ["gemma-4-12b"]


def test_test_my_setup_runs_in_the_background(engine, monkeypatch):
    monkeypatch.setattr("vault_ingest.app.setup_check", lambda s, manager=None: {"ok": True, "tokens_per_sec": 9.5})
    client = TestClient(make_app(engine, TOKEN), base_url="http://127.0.0.1")
    assert client.post("/settings/ai/test", headers=AUTH).json()["running"]
    deadline = time.time() + 5
    while engine.ai_test.get("running") and time.time() < deadline:
        time.sleep(0.01)
    assert client.get("/settings/ai", headers=AUTH).json()["test"] == {"ok": True, "tokens_per_sec": 9.5}
