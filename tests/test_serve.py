import asyncio
import dataclasses
import json
import os
import signal
import sys
import threading
import time

import httpx
import pytest
from starlette.testclient import TestClient

from vault_ingest import config
from vault_ingest.app import WATCH_RESTART_SECONDS, Engine, WatchChild
from vault_ingest.config import PROJECT_DIR
from vault_ingest.server import make_app, serve
from vault_ingest.watcher_meetings import Window, load_config

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SESSION = {"id": "m1", "app": "Google Meet", "window_title": "Meet - abc-defg-hij",
           "start": "2026-09-30T14:00:00+02:00", "end": "2026-09-30T14:50:00+02:00",
           "screenshots": [{"path": "_System/screenshots/2026-09-30/2026-09-30 14-05-00.png",
                            "timestamp": "2026-09-30T14:05:00+02:00", "offset": "00:05:00"}]}
SESSION_FILE = "_System/meetings/2026-09-30 14-00-00 Google Meet.json"


@pytest.fixture
def engine(vault, settings, tmp_path):
    cfg = dataclasses.replace(load_config(PROJECT_DIR / "companion.toml"), screenshot_folder=vault / "nope")
    return Engine(settings, cfg, data_dir=tmp_path / "home")


@pytest.fixture
def client(engine):
    return TestClient(make_app(engine, TOKEN), base_url="http://127.0.0.1")


def add_session(vault) -> None:
    (vault / "_System/meetings").mkdir(parents=True, exist_ok=True)
    (vault / SESSION_FILE).write_text(json.dumps(SESSION), encoding="utf-8")


def published(engine, action) -> list:
    """What an open /events stream receives while `action` runs."""
    async def run():
        queue = engine.events.subscribe()
        action()
        await asyncio.sleep(0)
        return [queue.get_nowait() for _ in range(queue.qsize())]

    return asyncio.run(run())


# ------------------------------------------------------------------ access
def test_requests_without_the_token_are_rejected(client):
    for path in ("/status", "/meetings", "/events", "/nothing-here"):
        assert client.get(path).status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/status?token=wrong").status_code == 401
    assert client.get("/status?token=").status_code == 401
    assert client.post(f"/status?token={TOKEN}").status_code == 401       # the query token is for GET only
    assert client.get("/status", headers=AUTH).status_code == 200
    for path in ("/status", "/meetings", "/notes", "/sessions/current"):
        assert client.get(f"{path}?token={TOKEN}").status_code == 401       # only /events takes it in the URL
    assert client.post(f"/events?token={TOKEN}").status_code == 401
    assert "access-control-allow-origin" not in client.get("/status", headers={**AUTH, "Origin": "http://x"}).headers


@pytest.mark.parametrize("origin", ["tauri://localhost", "http://tauri.localhost", "http://localhost:1420"])
def test_the_app_page_may_call_the_api_but_still_needs_the_token(client, origin):
    preflight = client.options("/status", headers={"Origin": origin, "Access-Control-Request-Method": "GET",
                                                   "Access-Control-Request-Headers": "authorization"})
    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == origin
    assert client.get("/status", headers={"Origin": origin}).status_code == 401
    ok = client.get("/status", headers={**AUTH, "Origin": origin})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == origin
    other = client.options("/status", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert other.status_code == 400 and "access-control-allow-origin" not in other.headers


def test_another_host_name_is_rejected(engine):
    rebound = TestClient(make_app(engine, TOKEN), base_url="http://evil.example")
    assert rebound.get("/status", headers=AUTH).status_code == 400


# ------------------------------------------------------------------ read endpoints
def test_status_shape(client, vault, engine):
    (vault / "00_Inbox" / "Acme - 2026-09-30 - Kickoff.txt").write_text("x", encoding="utf-8")
    (vault / "_System" / "ingest-status.json").write_text(
        json.dumps({"pid": 1, "file": "Acme - 2026-09-30 - Kickoff.txt", "step": "summary"}), encoding="utf-8")
    engine.gpu_state.update(paused=True, reason="a game", phase="paused")
    data = client.get("/status", headers=AUTH).json()
    assert set(data) == {"vault", "mode", "model", "inbox", "review", "job", "gpu", "watcher"}
    assert data["vault"] == str(vault) and data["mode"] == engine.s.effective_mode and data["model"]
    assert data["inbox"] == ["Acme - 2026-09-30 - Kickoff.txt"] and data["review"] == []
    assert data["job"] == {"file": "Acme - 2026-09-30 - Kickoff.txt", "step": "summary", "last_error": ""}
    assert data["gpu"]["paused"] is True and data["gpu"]["reason"] == "a game" and data["gpu"]["phase"] == "paused"
    assert data["watcher"] is None


def test_meetings_come_from_session_files_and_the_ingest_log(client, vault):
    assert client.get("/meetings", headers=AUTH).json() == {"calls": [], "sessions": [], "waiting": [],
                                                            "recordings": [], "recording": None}
    add_session(vault)
    record = {"client": "Acme", "date": "2026-09-30", "title": "Kickoff", "chunks": 4,
              "call_note": "Clients/Acme/Calls/2026-09-30 Kickoff.md", "processed_at": "2026-09-30T15:00:00",
              "generated_by": "cloud", "meeting_session": SESSION_FILE, "screenshots": 1, "raw": "not listed"}
    older = {**record, "date": "2026-09-01", "title": "Intro", "call_note": "Clients/Acme/Calls/2026-09-01 Intro.md",
             "meeting_session": None}
    (vault / "_System" / "ingest-log.json").write_text(
        json.dumps({"hash-older": older, "hash-text": record, "hash-audio": record}), encoding="utf-8")
    (vault / "00_Inbox" / "waiting.txt").write_text("x", encoding="utf-8")
    data = client.get("/meetings", headers=AUTH).json()
    assert [c["title"] for c in data["calls"]] == ["Kickoff", "Intro"]         # newest first, one per call note
    assert data["calls"][0]["session_id"] == "m1" and data["calls"][1]["session_id"] is None
    assert data["calls"][0]["client"] == "Acme" and "raw" not in data["calls"][0]
    assert [s["id"] for s in data["sessions"]] == ["m1"] and data["sessions"][0]["duration"] == 3000
    assert data["waiting"] == ["waiting.txt"]


def test_an_unreadable_ingest_log_lists_no_calls_and_writes_nothing(client, vault):
    (vault / "_System" / "ingest-log.json").write_text("{not json", encoding="utf-8")
    before = sorted(p.name for p in (vault / "_System").iterdir())
    assert client.get("/meetings", headers=AUTH).json()["calls"] == []
    assert sorted(p.name for p in (vault / "_System").iterdir()) == before


def test_one_meeting_with_its_screenshots(client, vault):
    add_session(vault)
    data = client.get("/sessions/m1", headers=AUTH).json()
    assert data["app"] == "Google Meet" and data["screenshots"][0]["offset"] == "00:05:00"
    assert client.get("/sessions/unknown", headers=AUTH).status_code == 404
    assert client.get("/sessions/current", headers=AUTH).json() is None


@pytest.mark.parametrize("sid", ["..", "%2e%2e", "..%2F..%2FPersonal%2Fdiary.md", "../../Personal/diary.md",
                                 "m1.json", "m1%00", "%2Fetc%2Fpasswd", "x" * 65])
def test_path_traversal_is_rejected(client, vault, sid):
    add_session(vault)
    response = client.get(f"/sessions/{sid}", headers=AUTH)
    assert response.status_code in (400, 404)
    assert "private" not in response.text and "Google Meet" not in response.text


def test_clients_list_and_search(client):
    assert client.get("/clients", headers=AUTH).json() == {
        "clients": [{"name": "Acme", "aliases": [], "contacts": ["Alex"], "meetings": 0, "last_meeting": None,
                     "open_items": 1, "profile": "Clients/Acme/Acme.md"}]}
    [hit] = client.get("/clients?q=alex", headers=AUTH).json()["clients"]
    assert hit["name"] == "Acme" and hit["via"] == "contact"
    assert client.get("/clients?q=zzzzzz", headers=AUTH).json() == {"clients": []}


def test_a_note_is_read_with_its_frontmatter(client, vault):
    calls = vault / "Clients/Acme/Calls"
    (calls / "2026-09-30 Kickoff.md").write_text(
        "---\ntype: call\nclient: Acme\ndate: 2026-09-30\nchunks: 2\n---\n# Kickoff\n\nČetvrtak.\n", encoding="utf-8")
    data = client.get("/notes", params={"path": "Clients/Acme/Calls/2026-09-30 Kickoff.md"}, headers=AUTH).json()
    assert data == {"path": "Clients/Acme/Calls/2026-09-30 Kickoff.md",
                    "frontmatter": {"type": "call", "client": "Acme", "date": "2026-09-30", "chunks": 2},
                    "body": "# Kickoff\n\nČetvrtak.\n", "links": []}
    assert client.get("/notes", params={"path": "Clients/Acme/Calls/nope.md"}, headers=AUTH).status_code == 404


@pytest.mark.parametrize("path", ["", "../outside.md", "Clients/../../outside.md", "/etc/passwd", "outside-link.md",
                                  "_System/ingest-log.json", "Clients/Acme/Calls", "a\x00.md", "."])
def test_notes_outside_the_vault_or_not_markdown_are_refused(client, vault, path):
    (vault.parent / "outside.md").write_text("secret", encoding="utf-8")
    (vault / "_System" / "ingest-log.json").write_text("{}", encoding="utf-8")
    (vault / "Clients/Acme/Calls").mkdir(parents=True, exist_ok=True)
    try:
        (vault / "outside-link.md").symlink_to(vault.parent / "outside.md")
    except OSError:
        pass                                                    # no symlinks here (Windows without the privilege)
    for route in ("/notes", "/backlinks"):
        response = client.get(route, params={"path": path}, headers=AUTH)
        assert response.status_code == 400 and "secret" not in response.text


def write_call(vault, name="2026-09-30 Kickoff", text="Četvrtak: the staging link."):
    path = vault / f"Clients/Acme/Calls/{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: call\nclient: Acme\ndate: {name[:10]}\n---\n# {name}\n\n"
                    f"**Client:** [[Clients/Acme/Acme|Acme]] · [[Gone]]\n\n{text}\n", encoding="utf-8")


def test_a_note_comes_with_its_links_resolved_and_its_backlinks(client, vault):
    write_call(vault)
    note = client.get("/notes", params={"path": "Clients/Acme/Calls/2026-09-30 Kickoff.md"}, headers=AUTH).json()
    assert [(x["target"], x["alias"], x["path"]) for x in note["links"]] == [
        ("Clients/Acme/Acme", "Acme", "Clients/Acme/Acme.md"), ("Gone", None, None)]
    [back] = client.get("/backlinks", params={"path": "Clients/Acme/Acme.md"}, headers=AUTH).json()["backlinks"]
    assert back["path"] == "Clients/Acme/Calls/2026-09-30 Kickoff.md" and back["type"] == "call"
    assert "[[Clients/Acme/Acme|Acme]]" in back["contexts"][0]


def test_search_endpoint(client, vault):
    write_call(vault)
    data = client.get("/search", params={"q": "cetvrtak"}, headers=AUTH).json()
    assert data["fts"] in (True, False)
    [hit] = data["results"]
    assert hit["path"] == "Clients/Acme/Calls/2026-09-30 Kickoff.md" and ["Četvrtak", True] in hit["snippet"]
    assert client.get("/search", params={"q": "cetvrtak", "types": "client"}, headers=AUTH).json()["results"] == []
    assert client.get("/search", params={"q": "x", "types": "nope"}, headers=AUTH).status_code == 400
    assert client.get("/search", params={"q": "x", "limit": "0"}, headers=AUTH).status_code == 400
    assert client.get("/search", params={"q": "x", "limit": "a"}, headers=AUTH).status_code == 400
    assert client.get("/search?q=x").status_code == 401


def test_graph_endpoint(client, vault):
    write_call(vault)
    (vault / "_System/Templates").mkdir(parents=True, exist_ok=True)
    (vault / "_System/Templates/Note.md").write_text("[[Clients/Acme/Acme]]", encoding="utf-8")
    graph = client.get("/graph", headers=AUTH).json()
    assert {n["id"] for n in graph["nodes"]} == {"Clients/Acme/Acme.md", "Clients/Acme/Calls/2026-09-30 Kickoff.md",
                                                 "Personal/diary.md"}                # a hand-written note, no _System
    assert graph["edges"] == [{"source": "Clients/Acme/Calls/2026-09-30 Kickoff.md", "target": "Clients/Acme/Acme.md"}]
    with_missing = client.get("/graph", params={"missing": "1", "client": "Acme"}, headers=AUTH).json()
    assert "missing:gone" in {n["id"] for n in with_missing["nodes"]}
    assert client.get("/graph", params={"types": "call"}, headers=AUTH).json()["edges"] == []
    assert client.get("/graph", params={"client": "Nobody"}, headers=AUTH).json() == {"nodes": [], "edges": []}
    assert client.get("/graph", params={"types": "raw"}, headers=AUTH).status_code == 400


# ------------------------------------------------------------------ events
def test_a_detected_call_is_published_instead_of_shown(engine):
    base = time.time()
    meet = [Window(1, 1, "chrome.exe", "Meet - abc-defg-hij - Google Chrome")]

    def call_starts_and_ends():
        engine.tick(base, meet)
        engine.tick(base + 3, meet)                              # second sighting: the meeting starts
        for k in range(2, 30):
            engine.tick(base + 3 * k, [])                        # the window is gone for over a minute

    events = published(engine, call_starts_and_ends)
    assert [name for name, _ in events] == ["meeting.started", "meeting.ended"]
    started, ended = events[0][1], events[1][1]
    assert started["app"] == "Google Meet" and started["end"] is None
    assert ended["id"] == started["id"] and ended["end"] is not None
    assert engine.store.find(started["id"]).end is not None        # the session file is still written


def test_progress_and_gpu_state_are_published_when_they_change(engine, vault):
    first = dict(published(engine, engine.poll_status))
    assert first["pipeline.progress"] == {"file": "", "step": "", "last_error": "", "inbox": 0, "review": 0}
    assert first["gpu.state"]["paused"] is False
    assert published(engine, engine.poll_status) == []            # nothing changed
    (vault / "00_Inbox" / "dropped.txt").write_text("x", encoding="utf-8")
    engine.gpu_state.update(paused=True, reason="a game")
    changed = dict(published(engine, engine.poll_status))
    assert changed["pipeline.progress"]["inbox"] == 1 and changed["gpu.state"]["reason"] == "a game"


def test_event_stream_over_http(client, engine):
    def feed():
        deadline = time.time() + 5
        while not engine.events._streams and time.time() < deadline:
            time.sleep(0.01)
        engine.events.publish("gpu.state", {"paused": True, "reason": "igra čeka"})
        engine.events.close()                                    # the stream ends when the engine stops

    feeder = threading.Thread(target=feed)
    feeder.start()
    response = client.get(f"/events?token={TOKEN}")
    feeder.join()
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert response.text == 'event: gpu.state\ndata: {"paused": true, "reason": "igra čeka"}\n\n'
    assert engine.events._streams == []
    assert client.get(f"/events?token={TOKEN}").text == ""        # a stream opened after the stop ends at once


# ------------------------------------------------------------------ watch child
class FakeProc:
    def __init__(self, pid):
        self.pid, self.code, self.terminated = pid, None, False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, -15

    def wait(self, timeout=None):
        return self.code


def test_watch_is_started_again_after_it_exits():
    started = []

    def popen(argv, **kwargs):
        started.append(FakeProc(len(started) + 1))
        return started[-1]

    child = WatchChild(["python", "-m", "vault_ingest", "watch", "--no-tray"], popen=popen)
    child.poll(100)
    child.poll(101)
    assert len(started) == 1                                     # running: left alone
    started[0].code = 1
    child.poll(102)
    child.poll(102 + WATCH_RESTART_SECONDS - 1)
    assert len(started) == 1                                     # not in a tight loop
    child.poll(102 + WATCH_RESTART_SECONDS)
    assert len(started) == 2
    child.stop()
    assert started[1].terminated and not started[0].terminated


def test_watch_that_cannot_start_is_tried_again():
    attempts = []

    def popen(argv, **kwargs):
        attempts.append(argv)
        raise OSError("no such interpreter")

    child = WatchChild(["missing"], popen=popen)
    child.poll(0)
    child.poll(1)
    child.poll(WATCH_RESTART_SECONDS)
    assert len(attempts) == 2 and child.proc is None


# ------------------------------------------------------------------ the process
def test_serve_prints_the_port_answers_and_stops_on_quit(engine, monkeypatch, capsys):
    seen = {}

    def shell():                                                 # stands in for the desktop app's end of stdin
        seen["ready"] = capsys.readouterr().out.strip()
        port = json.loads(seen["ready"].removeprefix("@@ ready "))["port"]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as http:
            seen["codes"] = [http.get("/status").status_code, http.get("/status", headers=AUTH).status_code]
        yield "tray click 1\n"                                   # a command of a later phase: ignored
        yield "quit\n"

    monkeypatch.setattr(sys, "stdin", shell())
    monkeypatch.setattr(signal, "signal", lambda *args: None)    # keep pytest's own SIGTERM handling
    monkeypatch.setattr(engine, "start", lambda lister: seen.update(started=True))
    assert serve(engine, None, TOKEN, show_token=False, parent_pid=os.getpid()) == 0
    assert seen["started"] and seen["codes"] == [401, 200]
    assert seen["ready"].startswith('@@ ready {"port": ') and TOKEN not in seen["ready"]


# ------------------------------------------------------------------ settings folder
def test_settings_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("VAULT_INGEST_HOME", raising=False)
    assert config.home() == PROJECT_DIR
    monkeypatch.setenv("VAULT_INGEST_HOME", str(tmp_path))
    assert config.home() == tmp_path.resolve()
