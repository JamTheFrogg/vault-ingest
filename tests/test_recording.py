"""Phase 3: recording through the desktop app, the call popup's API, the picker over HTTP, recording settings."""
import dataclasses
import datetime as dt
import json
import struct
import wave
from pathlib import Path

import pytest
from dotenv import dotenv_values
from starlette.testclient import TestClient

from vault_ingest.app import Engine, ShellLink
from vault_ingest.config import PROJECT_DIR, set_env_key, set_llm_mode
from vault_ingest.server import make_app
from vault_ingest.sessions import hms, iso, now_local, parse_iso
from vault_ingest.transcribe import RecordingQueue, Transcript, audio_tracks
from vault_ingest.vault import Vault
from vault_ingest.watcher_meetings import Window, load_config

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MEET = Window(hwnd=1, title="Meet - abc-defg-hij", process="chrome.exe", pid=10)


def wav(path: Path, seconds: float = 1.0) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * int(16000 * seconds))


@pytest.fixture
def lines():
    return []


@pytest.fixture
def engine(vault, settings, tmp_path, lines, monkeypatch):
    for key in ("AUTO_RECORD", "HIDE_POPUP_FROM_SHARING", "MIC_DEVICE", "MIC_DEVICE_NAME", "HOTKEY_RECORD"):
        monkeypatch.delenv(key, raising=False)
    cfg = dataclasses.replace(load_config(PROJECT_DIR / "companion.toml"), screenshot_folder=vault / "nope",
                              recordings_folder=tmp_path / "recordings", participant_screenshot=False,
                              start_after_matches=1, mic=dataclasses.replace(load_config(PROJECT_DIR / "companion.toml").mic, enabled=False))
    return Engine(settings, cfg, link=ShellLink(lines.append), env_file=tmp_path / "home" / ".env")


@pytest.fixture
def client(engine):
    return TestClient(make_app(engine, TOKEN), base_url="http://127.0.0.1")


def sent(lines, command):
    """The JSON of every `@@ <command>` line sent to the shell."""
    out = []
    for line in lines:
        head = f"@@ {command}"
        if line == head:
            out.append(None)
        elif line.startswith(head + " {"):
            out.append(json.loads(line[len(head) + 1:]))
    return out


# ------------------------------------------------------------------ .env and companion.toml
def test_set_env_key_rewrites_one_line_and_quotes_paths(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"VAULT_PATH=C:\\Vault\r\nAUTO_RECORD=false   # this computer\r\nLLM_MODE=cloud\r\n")
    set_env_key(env, "AUTO_RECORD", "true")
    set_env_key(env, "RECORDINGS_FOLDER", r"C:\Users\me\Music\vault ingest")
    set_env_key(env, "MIC_DEVICE_NAME", "Headset (Jabra's)")
    set_llm_mode(env, "local")
    raw = env.read_bytes()
    assert raw.startswith(b"VAULT_PATH=C:\\Vault\r\nAUTO_RECORD=true   # this computer\r\nLLM_MODE=local\r\n")
    values = dotenv_values(env)
    assert values["RECORDINGS_FOLDER"] == r"C:\Users\me\Music\vault ingest"     # backslashes survive
    assert values["MIC_DEVICE_NAME"] == "Headset (Jabra's)"
    set_env_key(env, "RECORDINGS_FOLDER", "/tmp/x")                               # a quoted value is replaced whole
    assert dotenv_values(env)["RECORDINGS_FOLDER"] == "/tmp/x" and raw.count(b"RECORDINGS_FOLDER") == 1
    for key, value in (("lower", "x"), ("A", "two\nlines")):
        with pytest.raises(ValueError):
            set_env_key(env, key, value)


def test_settings_in_env_win_over_companion_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("HOTKEY_RECORD", "ctrl+shift+r")
    monkeypatch.setenv("HOTKEY_POPUP", "")
    monkeypatch.setenv("HOTKEY_PAUSE", "ctrl+alt+p")
    monkeypatch.setenv("RECORDINGS_FOLDER", str(tmp_path / "rec"))
    cfg = load_config(PROJECT_DIR / "companion.toml")
    assert (cfg.record_hotkey, cfg.picker_hotkey, cfg.gpu.hotkey) == ("ctrl+shift+r", "", "ctrl+alt+p")
    assert cfg.mark_hotkey == "ctrl+alt+m" and cfg.hotkey == "ctrl+alt+s"
    assert cfg.recordings_folder == tmp_path / "rec"


def test_new_installs_record_into_their_own_folder():
    from vault_ingest.desktop import default_recordings_folder
    assert default_recordings_folder().parts[-2:] == ("Music", "vault-ingest")
    # the CLI companion still points at Meetily's folder (it records with Meetily)
    assert load_config(PROJECT_DIR / "companion.toml").recordings_folder.name == "meetily-recordings"


# ------------------------------------------------------------------ the queue takes the app's two tracks
def test_queue_waits_for_the_recorder_then_transcribes_both_tracks(vault, tmp_path, monkeypatch):
    from vault_ingest.config import Settings

    folder = tmp_path / "rec" / "2026-11-15 10-00-00 Google Meet"
    folder.mkdir(parents=True)
    wav(folder / "mic.wav"), wav(folder / "system.wav")
    (folder / "metadata.json").write_text(json.dumps({"meeting_name": "Acme - 2026-11-15 - Kickoff",
                                                      "created_at": "2026-11-15T10:00:00+01:00"}), encoding="utf-8")
    (folder / "recording.json").write_text(json.dumps({"finished": False, "marks": []}), encoding="utf-8")
    seen = []

    def fake(path, **kwargs):
        seen.append([(p.name, role) for p, role in audio_tracks(Path(path))])
        return Transcript("[00:00:00] **Me:** Hello.\n", "hr", "large-v3", "cpu", "int8", 1)

    monkeypatch.setattr("vault_ingest.transcribe.transcribe_path", fake)
    now = [0.0]
    queue = RecordingQueue(tmp_path / "rec", vault / "00_Inbox", vault / "_System/whisper-sources.json",
                           Vault(vault, ("00_Inbox", "_System")), Settings(vault_path=vault), settle_seconds=30,
                           clock=lambda: now[0])
    queue.poll()
    now[0] = 300
    queue.poll()
    assert queue._jobs == []                        # still recording: files that stand still 5 min are not taken
    (folder / "recording.json").write_text(json.dumps({"finished": True, "marks": [12.5]}), encoding="utf-8")
    queue.poll()
    assert queue._jobs == [folder] and queue.states() == {str(folder): "waiting"}   # finished: no settle wait
    queue._transcribe(queue._jobs.pop(0))
    assert seen == [[("mic.wav", "mic"), ("system.wav", "other")]]
    assert (vault / "00_Inbox" / "Acme - 2026-11-15 - Kickoff.txt").is_file()
    assert queue.states() == {}


def test_a_recording_the_app_never_finished_is_taken_after_ten_minutes(vault, tmp_path):
    from vault_ingest.config import Settings

    folder = tmp_path / "rec" / "crashed"
    folder.mkdir(parents=True)
    wav(folder / "mic.wav"), wav(folder / "system.wav")
    (folder / "recording.json").write_text('{"finished": false}', encoding="utf-8")
    now = [0.0]
    queue = RecordingQueue(tmp_path / "rec", vault / "00_Inbox", vault / "_System/whisper-sources.json",
                           Vault(vault, ("00_Inbox", "_System")), Settings(vault_path=vault), clock=lambda: now[0])
    queue.poll()
    now[0] = 599
    queue.poll()
    assert queue._jobs == []
    now[0] = 600
    queue.poll()
    assert queue._jobs == [folder]


# ------------------------------------------------------------------ record, mark, stop
def test_record_a_detected_call_end_to_end(engine, client, lines, vault):
    engine.companion.tick(1000.0, [MEET])
    session = engine.companion.session
    engine.meeting_map.put(session.meeting_key, "Acme")                  # a recurring meeting: Acme last time
    rec = client.post(f"/sessions/{session.id}/record", headers=AUTH).json()
    assert rec["state"] == "starting" and rec["session"] == session.id
    start = sent(lines, "record start")[0]
    assert start == {"folder": rec["folder"], "mic": {"kind": "default"}}
    folder = Path(rec["folder"])
    assert json.loads((folder / "metadata.json").read_text(encoding="utf-8"))["session"] == session.id
    assert session.client == "Acme" and session.recorded_prompt_clicked         # the chip's client was saved
    assert client.post("/recording/start", headers=AUTH).json()["folder"] == rec["folder"]   # one at a time

    engine.on_shell(f'recorder recording {json.dumps({"folder": str(folder), "seconds": 0})}')
    assert client.get("/recording", headers=AUTH).json()["state"] == "recording"
    client.post("/recording/mark", headers=AUTH)
    assert sent(lines, "record mark") == [None]
    engine.on_shell(f'recorder mark {json.dumps({"folder": str(folder), "at": 61.5})}')
    when = parse_iso(rec["started_at"]) + dt.timedelta(seconds=61.5)
    assert engine.recording["marks"] == [61.5]
    assert session.marks == [{"timestamp": iso(when), "offset": hms((when - session.start).total_seconds())}]
    rows = client.get("/meetings", headers=AUTH).json()["recordings"]
    assert [(r["state"], r["session"]) for r in rows] == [("recording", session.id)]

    client.post("/recording/stop", headers=AUTH)
    assert sent(lines, "record stop") == [None]
    engine.on_shell(f'recorder stopped {json.dumps({"folder": str(folder), "seconds": 2520.2})}')
    assert engine.recording["state"] == "stopped" and engine.recording["seconds"] == 2520
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["meeting_name"].startswith("Acme - ") and meta["meeting_name"].endswith(" - Google Meet")
    popup = client.get("/popup", headers=AUTH).json()
    assert popup["recording"]["state"] == "stopped" and popup["stage"] == "queued" and popup["live"] is True


def test_the_popup_follows_a_stopped_recording_to_its_note(engine, vault, tmp_path):
    folder = str(tmp_path / "recordings" / "r1")
    assert engine.recording_stage(folder) == "queued"
    sources = {"done": [folder], "sources": {"Acme - 2026-11-15 - Kickoff.txt": {"audio": f"{folder}/mic.wav"}}}
    (vault / "_System" / "whisper-sources.json").write_text(json.dumps(sources), encoding="utf-8")
    (vault / "00_Inbox" / "Acme - 2026-11-15 - Kickoff.txt").write_text("x", encoding="utf-8")
    assert engine.recording_stage(folder) == "writing"
    (vault / "00_Inbox" / "Acme - 2026-11-15 - Kickoff.txt").unlink()
    assert engine.recording_stage(folder) == "ready"
    (vault / "_System" / "whisper-sources.json").write_text(json.dumps({"done": [], "failed": {folder: {}}}), encoding="utf-8")
    assert engine.recording_stage(folder) == "failed"


def test_record_with_no_call_makes_a_meeting_of_its_own(engine, client, lines):
    rec = client.post("/recording/start", headers=AUTH).json()
    manual = engine.find_session(rec["session"])
    assert rec["manual"] and manual.app == "Recording" and manual.end is None
    engine.on_shell(f'recorder recording {json.dumps({"folder": rec["folder"]})}')
    assert client.get("/popup", headers=AUTH).json()["session"]["id"] == manual.id
    engine.on_shell(f'recorder stopped {json.dumps({"folder": rec["folder"], "seconds": 5})}')
    assert manual.end is not None and engine.store.find(manual.id).end is not None


def test_auto_record_starts_with_the_call_and_stops_when_it_ends(engine, lines):
    engine.s.auto_record = True
    engine.tick(1000.0, [MEET])
    assert len(sent(lines, "record start")) == 1
    assert sent(lines, "popup show")[-1] == {"session": engine.companion.session.id, "activate": False}
    engine.on_shell(f'recorder recording {json.dumps({"folder": engine.recording["folder"]})}')
    engine.tick(1000.0 + engine.cfg.end_grace_seconds + 5, [])
    assert sent(lines, "record stop") == [None]


def test_a_call_without_auto_record_only_shows_the_popup(engine, lines):
    engine.tick(1000.0, [MEET])
    assert sent(lines, "record start") == [] and sent(lines, "popup show")[0]["activate"] is False


def test_a_new_engine_adopts_the_recording_in_progress(engine, client, tmp_path):
    folder = tmp_path / "recordings" / "2026-11-15 10-00-00 Recording"
    folder.mkdir(parents=True)
    manual = engine.store.start("Recording", "Recording", "", now_local())
    engine.store.close_stale([manual])                  # what a new engine does with sessions left open
    (folder / "metadata.json").write_text(json.dumps({"meeting_name": "Recording", "session": manual.id,
                                                      "created_at": "2026-11-15T10:00:00+01:00"}), encoding="utf-8")
    engine.on_shell(f'recorder recording {json.dumps({"folder": str(folder), "seconds": 300})}')
    assert engine.recording_active() and engine.recording["session"] == manual.id and engine.recording["manual"]
    assert engine.store.find(manual.id).end is None
    assert engine.recording["started_at"] == "2026-11-15T10:00:00+01:00"
    engine.on_shell(f'recorder stopped {json.dumps({"folder": str(folder), "seconds": 400})}')
    assert engine.store.find(manual.id).end is not None


def test_recorder_errors_and_bad_lines(engine, client, lines):
    rec = client.post("/recording/start", headers=AUTH).json()
    engine.on_shell("recorder recording {not json")                          # logged, ignored
    engine.on_shell(f'recorder error {json.dumps({"folder": rec["folder"], "message": "mic.wav already exists"})}')
    assert engine.recording["state"] == "error" and engine.recording["message"] == "mic.wav already exists"
    assert not engine.recording_active()


def test_hotkeys_and_the_tray_from_the_shell(engine, lines, monkeypatch):
    snips = []
    monkeypatch.setattr("vault_ingest.app.open_snip_overlay", snips.append)
    engine.on_shell("hotkey screenshot")
    assert snips == [engine.cfg.screenshot_folder]
    engine.on_shell("hotkey record")
    assert len(sent(lines, "record start")) == 1
    engine.on_shell(f'recorder recording {json.dumps({"folder": engine.recording["folder"]})}')
    engine.on_shell("hotkey mark")
    engine.on_shell("hotkey record")
    assert sent(lines, "record mark") == [None] and sent(lines, "record stop") == [None]
    engine.on_shell("hotkey popup")
    assert sent(lines, "popup show")[-1]["card"] is True and sent(lines, "popup show")[-1]["activate"] is True
    engine.on_shell("tray click popup")
    assert sent(lines, "popup show")[-1]["activate"] is True
    engine.on_shell('shortcuts {"failed": {"mark": "already registered"}}')
    assert engine.recording_settings()["shortcut_failures"] == {"mark": "already registered"}


def test_whole_screen_screenshot_hides_the_popup_first(engine, client, lines, monkeypatch):
    shots = []
    monkeypatch.setattr("vault_ingest.app.take_participant_shot", lambda *a, **k: shots.append((a[3], k)))
    monkeypatch.setattr("vault_ingest.app.time.sleep", lambda s: None)
    engine.companion.tick(1000.0, [MEET])
    sid = engine.companion.session.id
    assert client.post(f"/sessions/{sid}/screenshot", headers=AUTH, json={"kind": "screen"}).json() == {"ok": True}
    assert shots == [("manual", {"asked": True})]
    assert lines.index("@@ popup hide") < max(i for i, line in enumerate(lines) if line.startswith("@@ popup show"))
    assert client.post(f"/sessions/{sid}/screenshot", headers=AUTH, json={"kind": "other"}).status_code == 400


# ------------------------------------------------------------------ the client picker over HTTP
def test_picker_over_http_uses_picker_logic(engine, client, vault):
    engine.companion.tick(1000.0, [MEET])
    sid = engine.companion.session.id
    view = client.post("/picker/open", headers=AUTH, json={"session": sid}).json()
    assert view["mode"] == "browse" and [r["kind"] for r in view["rows"]][-4:] == ["create", "personal", "unsorted", "skip"]
    assert view["headline"].startswith("Google Meet")
    ev = lambda kind, value=None: client.post("/picker/event", headers=AUTH,  # noqa: E731
                                              json={"session": sid, "type": kind, "value": value}).json()
    view = ev("query", "acm")
    assert view["rows"][0]["client"] == "Acme"
    ev("title", "Kickoff")
    done = ev("confirm")
    assert done["done"] and done["client"] == "Acme"
    session = engine.find_session(sid)
    assert (session.client, session.call_title) == ("Acme", "Kickoff")
    assert engine.meeting_map.get(session.meeting_key) == "Acme"

    # a new client: name, then contact / company / note, each skippable
    ev("query", "Bolt Studio")
    ev("select", len(ev("query", "Bolt Studio")["rows"]) - 4)               # the "New client" row
    assert ev("confirm")["mode"] == "contact"
    ev("submit_field", "Jane")
    ev("submit_field", "")
    done = ev("submit_field", "")
    assert done["done"] and done["client"] == "Bolt Studio" and (vault / "Clients" / "Bolt Studio").is_dir()
    assert client.post("/picker/event", headers=AUTH, json={"session": sid, "type": "nope"}).status_code == 400
    assert client.post("/picker/open", headers=AUTH, json={"session": "../x"}).status_code == 400
    assert client.post("/picker/open", headers=AUTH, content="[]").status_code == 400


def test_not_this_one_and_a_client_without_the_picker(engine, client):
    engine.companion.tick(1000.0, [MEET])
    sid = engine.companion.session.id
    data = client.patch(f"/sessions/{sid}", headers=AUTH, json={"skip": True}).json()
    assert data["skip"] is True and engine.companion.session.skip is True
    data = client.patch(f"/sessions/{sid}", headers=AUTH, json={"client": "Acme", "title": "Review"}).json()
    assert (data["client"], data["call_title"], data["skip"]) == ("Acme", "Review", False)
    assert client.patch(f"/sessions/{sid}", headers=AUTH, json={}).status_code == 400


# ------------------------------------------------------------------ settings (this computer)
def test_recording_settings_are_written_to_env_and_sent_to_the_shell(engine, client, lines, monkeypatch, tmp_path):
    for key in ("AUTO_RECORD", "HIDE_POPUP_FROM_SHARING", "MIC_DEVICE", "MIC_DEVICE_NAME", "HOTKEY_MARK"):
        monkeypatch.setenv(key, "x")                  # monkeypatch restores whatever the engine writes
        monkeypatch.delenv(key)
    data = client.get("/settings/recording", headers=AUTH).json()
    assert data["auto_record"] is False and data["hide_popup_from_sharing"] is True
    assert data["mic"] == {"kind": "default"} and data["hotkeys"]["record"] == "ctrl+alt+r"
    data = client.patch("/settings/recording", headers=AUTH, json={
        "auto_record": True, "hide_popup_from_sharing": False, "hotkeys": {"mark": "ctrl+shift+m"},
        "mic": {"kind": "device", "id": "{0.0.1.00000000}.{abc}", "name": "Headset (Jabra)"}}).json()
    assert data["auto_record"] is True and engine.s.auto_record is True and data["hotkeys"]["mark"] == "ctrl+shift+m"
    assert data["mic"] == {"kind": "device", "id": "{0.0.1.00000000}.{abc}", "name": "Headset (Jabra)"}
    env = dotenv_values(tmp_path / "home" / ".env")
    assert env["AUTO_RECORD"] == "true" and env["HIDE_POPUP_FROM_SHARING"] == "false"
    assert env["MIC_DEVICE_NAME"] == "Headset (Jabra)" and env["HOTKEY_MARK"] == "ctrl+shift+m"
    config = sent(lines, "config")[-1]
    assert config["content_protected"] is False and config["mic"]["kind"] == "device"
    assert config["hotkeys"]["mark"] == "ctrl+shift+m"
    for bad in ({"auto_record": "yes"}, {"mic": {"kind": "device"}}, {"hotkeys": {"mark": "rm -rf"}},
                {"hotkeys": {"nope": "ctrl+a"}}, {"mic": {"kind": "device", "id": "a\nB=1", "name": "x"}}):
        assert client.patch("/settings/recording", headers=AUTH, json=bad).status_code == 400
    assert client.patch("/settings/recording", headers=AUTH, json={"mic": {"kind": "communications"}}).json()["mic"] \
        == {"kind": "communications"}
    assert client.patch("/settings/recording", headers=AUTH, json={"hotkeys": {"mark": ""}}).json()["hotkeys"]["mark"] == ""


def test_the_app_page_may_post(client):
    preflight = client.options("/recording/start", headers={
        "Origin": "http://tauri.localhost", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type"})
    assert preflight.status_code == 200
    assert client.post("/recording/start").status_code == 401
