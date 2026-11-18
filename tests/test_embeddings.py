import dataclasses
import inspect
import json
import os
import sys
import threading

import pytest
from starlette.testclient import TestClient

from conftest import fake_vector
from vault_ingest import embeddings, llama_server, models
from vault_ingest.app import Engine
from vault_ingest.config import PROJECT_DIR
from vault_ingest.embeddings import EmbeddingClient, EmbeddingServer, EmbeddingUnavailable, chunk_note
from vault_ingest.index import NoteIndex
from vault_ingest.server import make_app
from vault_ingest.watcher_meetings import load_config

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# llama-server --embedding as a stub: the flags it got, /health, /v1/embeddings with the fake vectors
STUB = r'''#!PYTHON
import json, os, re, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
args = sys.argv[1:]
port, key = int(args[args.index("--port") + 1]), os.environ.get("LLAMA_API_KEY", "")
open(os.environ.get("STUB_ARGS", os.devnull), "w").write(json.dumps(args))
CONCEPTS = CONCEPTS_HERE
FAKE
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        self.reply(200 if self.path == "/health" else 404, {"status": "ok"})
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {key}":
            return self.reply(401, {"error": "Invalid API Key"})
        self.reply(200, {"data": [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(data["input"])]})
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def write(vault, rel, text):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def later(path):
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


@pytest.fixture
def notes(vault):
    write(vault, "Clients/Acme/Calls/2026-10-01 Billing.md", "---\ntype: call\nclient: Acme\ndate: 2026-10-01\n---\n"
          "# Billing\n\n## Summary\nThe invoice for September is still open. They paid the August one late.\n")
    write(vault, "Clients/Acme/Calls/2026-10-08 Launch.md", "---\ntype: call\nclient: Acme\ndate: 2026-10-08\n---\n"
          "# Launch\n\n## Summary\nThe staging server is ready; the release goes out Friday.\n\n"
          "## Notes\nWe also spoke about the logo.\n")
    write(vault, "Personal/Plans.md", "# Plans\n\nA holiday in June, a short break in May.\n")
    return vault


def embed_all(idx: NoteIndex, client: EmbeddingClient, model="fake") -> int:
    count = 0
    while rows := idx.to_embed(model):
        for note in rows:
            pieces = chunk_note(note["title"], note["body"] or "")
            idx.put_vectors(note, model, pieces, client.embed([embeddings.embed_input(note["title"], h, t)
                                                               for h, t in pieces]))
            count += 1
    return count


@pytest.fixture
def index(notes, tmp_path, fake_embedder):
    idx = NoteIndex(notes, tmp_path / "home" / "index.sqlite")
    idx.refresh()
    embed_all(idx, EmbeddingClient(fake_embedder.url, "fake"))
    yield idx
    idx.close()


# ------------------------------------------------------------------ passages
def test_a_note_is_cut_at_headings_and_packed_by_paragraph():
    body = "# Title\n\nIntro line one\nstill intro.\n\n## Summary\nFirst.\n\nSecond.\n\n### Detail\n" + "word " * 400
    pieces = chunk_note("Title", body, max_chars=200)
    assert pieces[0] == (None, "Intro line one still intro.")
    assert pieces[1] == ("Summary", "First.\n\nSecond.")
    assert all(h == "Detail" and len(t) <= 200 for h, t in pieces[2:]) and len(pieces) > 4
    assert " ".join(t for _h, t in pieces[2:]).split() == ["word"] * 400       # nothing lost
    assert chunk_note("Empty note", "") == [(None, "Empty note")]
    assert embeddings.embed_input("Billing", "Summary", "The invoice.") == "Billing\nSummary\n\nThe invoice."


# ------------------------------------------------------------------ the client
def test_the_client_batches_and_reports_a_server_that_is_gone(fake_embedder):
    client = EmbeddingClient(fake_embedder.url, "fake")
    vectors = client.embed([f"invoice {i}" for i in range(20)])
    assert len(vectors) == 20 and vectors[0][0] == 1.0 and len(fake_embedder.requests) == 20
    with pytest.raises(EmbeddingUnavailable):
        EmbeddingClient("http://127.0.0.1:9/v1", "fake", timeout=2).embed(["x"])


# ------------------------------------------------------------------ ranking
def test_meaning_finds_a_note_without_the_words(index, fake_embedder):
    vector = fake_vector("payment")
    [first, *_] = index.semantic(vector, "fake", 3, query="payment")
    assert first["path"] == "Clients/Acme/Calls/2026-10-01 Billing.md" and first["heading"] == "Summary"
    assert "invoice" in first["passage"] and first["cosine"] > 0.9
    assert index.search("payment") == []                                  # keyword search cannot find it
    rest = index.semantic(fake_vector("vacation"), "fake", 1)
    assert rest[0]["path"] == "Personal/Plans.md"
    assert {r["type"] for r in index.semantic(fake_vector("vacation"), "fake", 5, types=("call",))} == {"call"}
    assert index.semantic(vector, "another-model", 5) == []                # vectors of one model only
    with pytest.raises(ValueError, match="dimensions"):
        index.semantic([1.0, 0.0], "fake", 3)


def test_lexical_prefetch_ranks_only_keyword_candidates_and_alpha_blends(index):
    query = "release logo"
    # every note is a candidate for "release" or "logo" only if it has one of the words: Billing has neither
    hybrid = index.semantic(fake_vector("invoice"), "fake", 5, query=query, lexical=True)
    assert [r["path"] for r in hybrid] == ["Clients/Acme/Calls/2026-10-08 Launch.md"]
    # alpha 1: keyword order; alpha 0: meaning order, within the same candidates
    keyword_only = index.semantic(fake_vector("invoice"), "fake", 5, query="invoice holiday", lexical=True, alpha=1.0)
    meaning_only = index.semantic(fake_vector("vacation"), "fake", 5, query="invoice holiday", lexical=True, alpha=0.0)
    assert {r["path"] for r in keyword_only} == {r["path"] for r in meaning_only} == {
        "Clients/Acme/Calls/2026-10-01 Billing.md", "Personal/Plans.md"}
    assert meaning_only[0]["path"] == "Personal/Plans.md"
    assert all(0 <= r["score"] <= 1 for r in keyword_only + meaning_only)


def test_vectors_follow_the_notes(index, notes, fake_embedder):
    client = EmbeddingClient(fake_embedder.url, "fake")
    assert index.to_embed("fake") == [] and index.embedded_count("fake") == {"done": 5, "total": 5}
    sent = len(fake_embedder.requests)
    plans = notes / "Personal/Plans.md"
    plans.write_text("# Plans\n\nPay the invoice before the holiday.\n", encoding="utf-8")
    later(plans)
    index.refresh()
    assert [r["path"] for r in index.to_embed("fake")] == ["Personal/Plans.md"]
    assert index.embedded_count("fake") == {"done": 4, "total": 5}
    # until it is embedded again its last vectors still count (last known good)
    assert any(r["path"] == "Personal/Plans.md" for r in index.semantic(fake_vector("vacation"), "fake", 3))
    assert embed_all(index, client) == 1 and len(fake_embedder.requests) == sent + 1
    (notes / "Clients/Acme/Calls/2026-10-08 Launch.md").unlink()
    index.refresh()
    assert index.embedded_count("fake") == {"done": 4, "total": 4}
    assert index._rows("SELECT count(*) AS n FROM chunks c LEFT JOIN notes n ON n.id = c.note WHERE n.id IS NULL")[0]["n"] == 0
    assert all("Launch" not in r["path"] for r in index.semantic(fake_vector("staging"), "fake", 5))


def test_a_second_process_reads_while_one_writes(index, notes):
    other = NoteIndex(notes, index.db_path)
    try:
        assert other.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert other.semantic(fake_vector("payment"), "fake", 1)[0]["path"].endswith("Billing.md")
        for i in range(30):
            write(notes, f"Work/Note {i}.md", f"# Note {i}\n\nAbout the invoice {i}.\n")
        errors = []

        def run(idx):
            try:
                idx.refresh()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=run, args=(x,)) for x in (index, other)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert index.embedded_count("fake")["total"] == other.embedded_count("fake")["total"] == 35
        assert len({r["path"] for r in other._rows("SELECT path FROM notes")}) == 35
    finally:
        other.close()


# ------------------------------------------------------------------ the server process
@pytest.fixture
def embed_home(tmp_path):
    cat = models.load_catalogue()
    root = tmp_path / "home"
    build = models.entry(cat, "llama-linux-cpu")
    exe_dir = models.folder(build, root, cat) / "llama-b12380"
    exe_dir.mkdir(parents=True)
    exe = exe_dir / "llama-server"
    fake = inspect.getsource(fake_vector)
    stub = STUB.replace("PYTHON", sys.executable).replace("CONCEPTS_HERE", repr(__import__("conftest").CONCEPTS))
    exe.write_text(stub.replace("FAKE", fake), encoding="utf-8")
    exe.chmod(0o755)
    (exe_dir.parent / ".complete").write_text(json.dumps({"server": "llama-b12380/llama-server"}), encoding="utf-8")
    model = models.entry(cat, "bge-m3")
    folder = models.folder(model, root, cat)
    folder.mkdir(parents=True)
    (folder / model["files"][0]["name"]).write_bytes(b"gguf")
    (folder / ".complete").write_text("{}", encoding="utf-8")
    return root


@pytest.mark.skipif(os.name == "nt", reason="the stub is a shebang script")
def test_the_embedding_server_runs_on_the_processor_and_leaves_the_chat_settings(settings, embed_home, tmp_path,
                                                                                 monkeypatch):
    monkeypatch.setenv("STUB_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setattr(models, "this_os", lambda system=None: "linux")
    before = settings.openai_compat_base_url
    server = EmbeddingServer(settings, root=embed_home)
    try:
        info = server.start()
        args = json.loads((tmp_path / "args.json").read_text())
        assert args[:2] == ["-m", str(embed_home / "models" / "bge-m3" / "bge-m3-Q8_0.gguf")]
        for flag, value in (("-ngl", "0"), ("--device", "none"), ("--alias", "bge-m3"), ("-ub", "2048")):
            assert args[args.index(flag) + 1] == value
        assert "--embedding" in args and settings.openai_compat_base_url == before
        assert (embed_home / "state" / "llama-embed.json").is_file()
        assert not (embed_home / "state" / "llama-server.json").exists()       # the chat server's file is its own
        # a reader (the MCP server) finds it through the state file and never starts one
        client = embeddings.running_client(embed_home, server.cat)
        assert client is not None and client.embed(["invoice"])[0][0] == 1.0
        assert embeddings.model_id(embed_home, server.cat) == "bge-m3"
    finally:
        server.stop()
    assert not llama_server._is_llama(info["pid"])
    assert embeddings.running_client(embed_home, server.cat) is None


def test_search_by_meaning_is_off_until_downloaded_or_switched_off(tmp_path, monkeypatch):
    cat = models.load_catalogue()
    for key in ("EMBEDDINGS", "EMBEDDING_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    assert embeddings.model_id(tmp_path, cat) is None and embeddings.running_client(tmp_path, cat) is None
    with pytest.raises(EmbeddingUnavailable, match="not downloaded"):
        EmbeddingServer(root=tmp_path, catalogue=cat).start()
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-bge-m3")
    assert embeddings.model_id(tmp_path, cat) == "address:text-embedding-bge-m3"
    monkeypatch.setenv("EMBEDDINGS", "off")
    assert embeddings.model_id(tmp_path, cat) is None and embeddings.running_client(tmp_path, cat) is None


def test_the_catalogue_names_an_embedding_model_that_exists():
    cat = models.load_catalogue()
    assert models.entry(cat, cat["embedding"])["kind"] == "embedding"
    assert models.validate({**cat, "embedding": "gemma-4-e4b"}) == ["embedding must name a embedding entry"]
    assert models.embedding_server_entry(cat, "windows")["id"] == "llama-win-cpu"
    assert models.embedding_server_entry(cat, "macos")["id"] == "llama-macos-metal"


# ------------------------------------------------------------------ the engine
@pytest.fixture
def engine(notes, settings, tmp_path, monkeypatch, fake_embedder):
    monkeypatch.setenv("EMBEDDING_BASE_URL", fake_embedder.url)
    monkeypatch.setenv("EMBEDDING_MODEL", "fake")
    monkeypatch.delenv("EMBEDDINGS", raising=False)
    cfg = dataclasses.replace(load_config(PROJECT_DIR / "companion.toml"), screenshot_folder=notes / "nope")
    e = Engine(settings, cfg, data_dir=tmp_path / "home")
    yield e
    if e._index is not None:
        e._index.close()


def test_the_engine_embeds_what_is_new_and_nothing_twice(engine, notes, fake_embedder):
    events = []
    engine.events.publish = lambda name, data: events.append((name, data))
    engine.embed_pending()
    first = len(fake_embedder.requests)
    assert first >= 5 and engine.embedding == {"state": "ready", "done": 5, "total": 5, "message": None}
    assert ("embeddings.progress", {"state": "indexing", "done": 0, "total": 5, "message": None}) in events
    engine.embed_pending()
    assert len(fake_embedder.requests) == first                           # nothing changed: nothing sent
    later(write(notes, "Personal/Plans.md", "# Plans\n\nThe brand colours.\n"))
    engine.index.refresh()
    engine.embed_pending()
    assert fake_embedder.requests[first:] == ["Plans\n\nThe brand colours."]


def test_search_endpoint_by_meaning(engine, fake_embedder):
    client = TestClient(make_app(engine, TOKEN), base_url="http://127.0.0.1")
    r = client.get("/search", params={"q": "payment", "semantic": "1"}, headers=AUTH)
    assert r.status_code == 409 and r.json()["semantic"]["state"] == "off"   # not started yet
    engine.embed_pending()
    data = client.get("/search", params={"q": "payment", "semantic": "1"}, headers=AUTH).json()
    assert data["results"][0]["path"] == "Clients/Acme/Calls/2026-10-01 Billing.md"
    assert data["semantic"]["state"] == "ready"
    plain = client.get("/search", params={"q": "invoice"}, headers=AUTH).json()
    assert plain["semantic"]["done"] == 5 and plain["results"][0]["path"].endswith("Billing.md")


def test_downloading_search_by_meaning_also_gets_the_processor_build(engine, monkeypatch):
    started = []
    monkeypatch.setattr(engine.downloads, "start", lambda entry_id: started.append(entry_id) or {"id": entry_id})
    monkeypatch.setattr(models, "this_os", lambda system=None: "windows")
    engine.download("bge-m3", "start")
    assert started == ["llama-win-cpu", "bge-m3"]
