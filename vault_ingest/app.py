"""The engine behind `vault-ingest serve`: meeting detection without a window or tray, the `watch` child,
and the events the UI listens to. The window, tray and hotkeys belong to the desktop app."""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import connect, embeddings
from .cli import _client_names, _names, _read_job
from .clients import MeetingMap, load_clients, meeting_key
from .config import DEFAULT_ENV_FILE, Settings, home, set_env_key
from .gpu import STATE_NAME, GpuStateStore
from .index import NoteIndex
from .llama_server import engine_name, setup_check
from .models import (Downloads, chat_entry, embedding_server_entry, entry, for_machine, hardware, installed,
                     load_catalogue)
from .picker import Created, DecideLater, PickerState, Select, Skip, apply_picker_result
from .picker_ui import headline, person_guess
from .sessions import Session, SessionStore, hms, iso, now_local, parse_iso
from .vault import Vault, sanitize_name
from .watcher_meetings import (LM_RETRY_SECONDS, Companion, CompanionConfig, Ended, open_snip_overlay,
                               take_participant_shot)

log = logging.getLogger("vault_ingest")
WATCH_RESTART_SECONDS = 10
STATUS_POLL_SECONDS = 1
EMBED_IDLE_SECONDS = 300               # a look for notes without vectors even when no event came
EMBED_RETRY_SECONDS = 60


class Events:
    """Hands each published event to every open `/events` stream. `publish` may be called from any thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._streams: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._closed = False

    def subscribe(self) -> asyncio.Queue:
        """Call inside the server's event loop. The queue gives (event, data) pairs, then None when the engine stops."""
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            if self._closed:
                queue.put_nowait(None)
            else:
                self._streams.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._streams = [(loop, q) for loop, q in self._streams if q is not queue]

    def publish(self, event: str, data) -> None:
        self._send((event, data))

    def close(self) -> None:
        """End every open stream; without this the server waits for them forever when it shuts down."""
        with self._lock:
            self._closed = True
        self._send(None)

    def _send(self, item) -> None:
        with self._lock:
            streams = list(self._streams)
        for loop, queue in streams:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:                          # that stream's loop has already closed
                self.unsubscribe(queue)


class ShellLink:
    """Commands to the desktop app, one line each on stdout: `@@ <command> [json]`. Without the app (a terminal,
    a background task) nothing is written."""

    def __init__(self, write: Callable[[str], None] | None = None):
        self._write = write
        self._lock = threading.Lock()

    def send(self, command: str, data=None) -> None:
        if self._write is None:
            return
        line = f"@@ {command}" + ("" if data is None else f" {json.dumps(data, ensure_ascii=False)}")
        with self._lock:
            self._write(line)


HOTKEY_ACTIONS = ("popup", "screenshot", "record", "mark", "pause")
HOTKEY_KEYS = {"popup": "HOTKEY_POPUP", "screenshot": "HOTKEY_SCREENSHOT", "record": "HOTKEY_RECORD",
               "mark": "HOTKEY_MARK", "pause": "HOTKEY_PAUSE"}
ADDRESS = re.compile(r"https?://[A-Za-z0-9.\[\]:-]+(?::\d{1,5})?(?:/v1)?")
COMBO = re.compile(r"(?:(?:ctrl|alt|shift|super|cmd|option)\+){1,3}[a-z0-9]", re.I)


class WatchChild:
    """`vault-ingest watch` as a child process, started again when it exits."""

    def __init__(self, argv: list[str], popen: Callable = subprocess.Popen):
        self.argv = argv
        self.popen = popen
        self.proc = None
        self.next_start = 0.0

    def poll(self, now: float) -> None:
        if self.proc is not None:
            code = self.proc.poll()
            if code is None:
                return
            log.warning("watch exited (code %s); starting it again in %d seconds. If another watcher is already "
                        "running, stop it.", code, WATCH_RESTART_SECONDS)
            self.proc = None
            self.next_start = now + WATCH_RESTART_SECONDS
        if now < self.next_start:
            return
        try:
            # stdin and stdout are not shared: the desktop app talks to `serve` over them. watch logs to ingest.log.
            self.proc = self.popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            log.warning("Could not start watch (%s); trying again in %d seconds.", e, WATCH_RESTART_SECONDS)
            self.next_start = now + WATCH_RESTART_SECONDS
            return
        log.info("Started watch (pid %s).", self.proc.pid)

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


class Engine:
    """`Companion` detection plus the status files `watch` writes, published as events."""

    def __init__(self, s: Settings, cfg: CompanionConfig, *, audio: Callable[[], set[str]] | None = None,
                 mic=None, gaming=None, watch: WatchChild | None = None, link: ShellLink | None = None,
                 env_file: Path | None = None, data_dir: Path | None = None):
        self.s, self.cfg = s, cfg
        self.vault = Vault(s.vault_path, (s.inbox_dir, s.system_dir))
        self.store = SessionStore(s.system / "meetings", self.vault)
        self.gpu_state = GpuStateStore(s.system / STATE_NAME)
        self.events = Events()
        self.watch = watch
        self.link = link or ShellLink()
        self.env_file = Path(env_file or DEFAULT_ENV_FILE)
        self.data_dir = Path(data_dir or home())          # models/, bin/llama/, pylibs/ of the built-in AI
        self.catalogue = load_catalogue()
        self.downloads = Downloads(self.data_dir, self.catalogue, self._download_progress)
        self._hardware: dict | None = None
        self.ai_test: dict | None = None
        self._ai_pending = False                          # AI settings changed; applied when no job runs
        self.companion = Companion(
            s, cfg, self.vault, self.store, audio=audio, mic=mic, gaming=gaming,
            on_started=self._meeting_started,
            on_retitle=lambda session: self.events.publish("meeting.updated", session.to_json()))
        self.meeting_map = MeetingMap(s.system / "meeting-map.json", self.vault)
        self.gpu = None
        self.recordings = None
        # the recording the desktop app makes now, or made last: what the popup and the Meetings list show
        self.recording: dict | None = None
        self._manual: Session | None = None        # a recording started with no call detected
        self._lock = threading.RLock()
        self.pickers: dict[str, PickerState] = {}
        self.shortcut_failures: dict[str, str] = {}
        self._published: dict[str, dict] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._index: NoteIndex | None = None
        self._index_lock = threading.Lock()
        self.embed_server = embeddings.EmbeddingServer(s, root=self.data_dir, catalogue=self.catalogue)
        self.embedder: embeddings.EmbeddingClient | None = None
        self.embedding = {"state": "off", "done": 0, "total": 0, "message": None}
        self._embed_wake = threading.Event()
        self._server_check: tuple[float, str | None] | None = None
        self.connect_paths: dict[str, Path] = {}          # tests point the three config files elsewhere

    # ---------------------------------------------------------------- one round each
    def tick(self, now: float, windows: list) -> None:
        live = self.companion.session
        for ev in self.companion.tick(now, windows):
            if isinstance(ev, Ended) and live is not None:
                self.events.publish("meeting.ended", live.to_json())
                if self.recording_active() and self.recording["session"] == live.id:
                    self.stop_recording()             # the call is over: so is its recording

    def _meeting_started(self, session: Session) -> None:
        self.events.publish("meeting.started", session.to_json())
        if self.s.auto_record:
            self.record(session)
        if self.cfg.popup_enabled:
            self.link.send("popup show", {"session": session.id, "activate": False})

    # ---------------------------------------------------------------- recording (the desktop app records)
    def recording_active(self) -> bool:
        return self.recording is not None and self.recording["state"] in ("starting", "recording")

    def find_session(self, sid: str) -> Session | None:
        """The live object when the companion holds it (its later writes must not undo a change), else the file."""
        for x in [*self.companion.sessions, self._manual]:
            if x is not None and x.id == sid:
                return x
        return self.store.find(sid)

    def mic_choice(self) -> dict:
        device = self.s.mic_device
        if device in ("default", "communications", ""):
            return {"kind": device or "default"}
        return {"kind": "device", "id": device, "name": self.s.mic_device_name}

    def record(self, session: Session | None = None) -> dict:
        """Start recording `session`, the call in progress, or (no call) a meeting of its own. The shell does the
        recording and reports back over stdin (`on_shell`)."""
        with self._lock:
            if self.recording_active():
                return self.recording
            now = now_local()
            session = session or self.companion.session
            manual = session is None
            if session is None:
                session = self._manual = self.store.start("Recording", "Recording", "", now)
            self.link.send("popup hide")          # the participant screenshot must not show the popup
            take_participant_shot(self.companion, self.cfg, home() / "state" / "captures")
            if not manual:
                self.link.send("popup show", {"session": session.id, "activate": False})
            remembered = self.meeting_map.get(session.meeting_key)
            if session.client is None and remembered:                 # the client shown on the chip is saved
                apply_picker_result(Select(remembered, session.call_title or ""), session, self.store, self.s,
                                    self.meeting_map)
            self.store.update(session, recorded_prompt_clicked=True)
            folder = self.cfg.recordings_folder / f"{now:%Y-%m-%d %H-%M-%S} {sanitize_name(session.app)}"
            folder.mkdir(parents=True, exist_ok=True)
            self._write_metadata(folder, session, now)
            self.recording = {"state": "starting", "folder": str(folder), "session": session.id, "manual": manual,
                              "started_at": iso(now), "ended_at": None, "seconds": 0, "marks": [], "message": None}
            self.link.send("record start", {"folder": str(folder), "mic": self.mic_choice()})
            if not manual and self.cfg.participant_screenshot and self.cfg.participant_followup_seconds > 0:
                timer = threading.Timer(self.cfg.participant_followup_seconds, self._followup_shot, args=(session,))
                timer.daemon = True
                timer.start()
        log.info("Recording %s into %s", session.app, folder)
        self._publish_recording()
        return self.recording

    def _followup_shot(self, session: Session) -> None:
        """People who join late: one more screenshot while the same recording runs."""
        if self.recording_active() and self.recording["session"] == session.id:
            self.link.send("popup hide")
            take_participant_shot(self.companion, self.cfg, home() / "state" / "captures")
            self.link.send("popup show", {"session": session.id, "activate": False})

    def _write_metadata(self, folder: Path, session: Session, created: dt.datetime) -> None:
        """What `RecordingQueue` names the transcript after. With a client chosen it is the full
        `<Client> - YYYY-MM-DD - <Title>` name, so the note is filed even without the session match."""
        title = session.call_title or session.app
        name = f"{session.client} - {created.date().isoformat()} - {title}" if session.client else title
        data = {"meeting_name": name, "created_at": iso(created), "session": session.id}
        (folder / "metadata.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def stop_recording(self) -> dict | None:
        if self.recording_active():
            self.link.send("record stop")
        return self.recording

    def mark(self) -> dict | None:
        if self.recording_active():
            self.link.send("record mark")
        return self.recording

    def screenshot(self, kind: str) -> None:
        """`region`: the snip tool, its file is attached by the screenshot watcher. `screen`: the whole desktop."""
        if kind == "region":
            open_snip_overlay(self.cfg.screenshot_folder)
            return
        self.link.send("popup hide")
        time.sleep(0.35)                          # let the window manager take the popup away first
        take_participant_shot(self.companion, self.cfg, home() / "state" / "captures", "manual", asked=True)
        live = self.companion.latest_session() or self._manual
        self.link.send("popup show", {"session": live.id if live else None, "activate": False})

    def show_popup(self, activate: bool, card: bool = False) -> None:
        live = self.companion.latest_session() or (self._manual if self.recording_active() else None)
        self.link.send("popup show", {"session": live.id if live else None, "activate": activate, "card": card})

    def on_shell(self, line: str) -> None:
        """A line from the desktop app on stdin: `recorder <state> {json}`, `hotkey <action>`, `tray click popup`,
        `shortcuts {json}`."""
        word, _, rest = line.strip().partition(" ")
        try:
            if word == "recorder":
                state, _, payload = rest.partition(" ")
                self._recorder_event(state, json.loads(payload or "{}"))
            elif word == "hotkey":
                self.hotkey(rest.strip())
            elif word == "tray" and rest.strip() == "click popup":
                self.show_popup(activate=True)
            elif word == "shortcuts":
                self.shortcut_failures = dict(json.loads(rest).get("failed") or {})
                self.events.publish("settings.changed", {"shortcut_failures": self.shortcut_failures})
        except (ValueError, AttributeError, TypeError) as e:
            log.warning("Unreadable line from the desktop app (%s): %r", e, line[:200])

    def hotkey(self, action: str) -> None:
        if action == "popup":
            self.show_popup(activate=True, card=True)
        elif action == "screenshot":
            open_snip_overlay(self.cfg.screenshot_folder)
        elif action == "record":
            self.stop_recording() if self.recording_active() else self.record()
        elif action == "mark":
            self.mark()
        elif action == "pause" and self.gpu is not None:
            self.gpu.toggle()

    def _recorder_event(self, state: str, data: dict) -> None:
        folder = str(data.get("folder") or "")
        with self._lock:
            rec = self.recording
            if state == "recording" and (rec is None or rec["folder"] != folder):
                rec = self.recording = self._adopt(Path(folder), data)        # this engine started after the recording
            if rec is None or (folder and rec["folder"] != folder):
                return
            session = self.find_session(rec["session"]) if rec.get("session") else None
            if state == "recording":
                rec["state"] = "recording"
            elif state == "mark":
                at = float(data.get("at") or 0)
                rec["marks"].append(at)
                when = parse_iso(rec["started_at"]) + dt.timedelta(seconds=at)
                if session is not None:
                    self.store.update(session, marks=[*session.marks, {
                        "timestamp": iso(when), "offset": hms((when - session.start).total_seconds())}])
            elif state == "stopped":
                ended = now_local()
                rec.update(state="stopped", ended_at=iso(ended), seconds=round(float(data.get("seconds") or 0)))
                if session is not None:
                    if rec.get("manual"):
                        self.store.update(session, end=ended, last_seen=ended)
                        self._manual = None
                    self._write_metadata(Path(rec["folder"]), session, parse_iso(rec["started_at"]))
                log.info("Recording finished: %s (%ss)", rec["folder"], rec["seconds"])
            elif state == "error":
                rec.update(state="error", message=str(data.get("message") or "The recorder failed."))
                log.error("Recording failed: %s", rec["message"])
        self._publish_recording()

    def _adopt(self, folder: Path, data: dict) -> dict:
        """A recording that was already running when this engine started (the engine restarted mid-call)."""
        try:
            meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        session = self.store.find(str(meta.get("session") or "")) if meta.get("session") else None
        if session is not None and session.app == "Recording":
            self._manual = session
            if session.end is not None:           # closed as stale when this engine started: it is still going
                self.store.update(session, end=None)
        started = parse_iso(meta.get("created_at")) or now_local() - dt.timedelta(seconds=float(data.get("seconds") or 0))
        return {"state": "recording", "folder": str(folder), "session": session.id if session else None,
                "manual": self._manual is not None, "started_at": iso(started), "ended_at": None, "seconds": 0,
                "marks": [], "message": None}

    def _publish_recording(self) -> None:
        self.events.publish("recording.state", self.recording)

    def recording_rows(self) -> list[dict]:
        """Recordings that have no note yet: the one being recorded, and those waiting for or in transcription."""
        rows = []
        states = dict(self.recordings.states()) if self.recordings is not None else {}
        if self.recording_active():
            states[self.recording["folder"]] = "recording"
        for folder, state in states.items():
            try:
                meta = json.loads((Path(folder) / "metadata.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            rows.append({"folder": folder, "state": state, "title": meta.get("meeting_name") or Path(folder).name,
                         "session": meta.get("session"), "started_at": meta.get("created_at")})
        return rows

    def recording_stage(self, folder: str) -> str:
        """`queued` (not taken yet), `waiting`, `transcribing`, `failed`, `writing` (its transcript is in the
        inbox), `review`, or `ready`."""
        states = self.recordings.states() if self.recordings is not None else {}
        if folder in states:
            return states[folder]
        try:
            done = json.loads((self.s.system / "whisper-sources.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            done = {}
        if folder in (done.get("failed") or {}):
            return "failed"
        if folder not in (done.get("done") or []):
            return "queued"
        names = [n for n, src in (done.get("sources") or {}).items()
                 if isinstance(src, dict) and Path(str(src.get("audio", ""))).parent == Path(folder)]
        if any(n in _names(self.s.inbox) for n in names):
            return "writing"
        if any(n in _names(self.s.review) for n in names):
            return "review"
        return "ready"

    def popup(self) -> dict:
        """Everything the call popup draws: the meeting, the recording, and where its note is."""
        live = self.companion.session
        session = live or self.companion.latest_session() or self._manual
        rec = self.recording
        if rec is not None and rec.get("session") and (session is None or session.id != rec["session"]):
            session = self.find_session(rec["session"]) or session
        line = headline(session)[1] if session is not None else ""
        stage = self.recording_stage(rec["folder"]) if rec is not None and rec["state"] == "stopped" else None
        return {"session": session.to_json() if session else None, "headline": line, "live": live is not None,
                "remembered": self.meeting_map.get(session.meeting_key) if session else None,
                "recording": rec, "stage": stage, "auto_record": self.s.auto_record}

    # ---------------------------------------------------------------- client picker (picker.py over HTTP)
    def picker_open(self, session: Session) -> dict:
        key = session.meeting_key or meeting_key(session.app, session.window_title)
        state = PickerState(load_clients(self.s.clients), preselect=session.client or self.meeting_map.get(key),
                            title=session.call_title or "", person=person_guess(session))
        self.pickers[session.id] = state
        return self._picker_view(session, state)

    def picker_event(self, session: Session, kind: str, value) -> dict:
        state = self.pickers.get(session.id) or (self.picker_open(session) and self.pickers[session.id])
        action = None
        if kind == "query":
            state.set_query(str(value or ""))
        elif kind == "move":
            state.move(int(value or 0))
        elif kind == "select" and state.mode == "browse":
            state.selected = max(0, min(len(state.rows) - 1, int(value or 0)))
        elif kind == "title":
            state.title = str(value or "")
        elif kind == "confirm":
            action = state.confirm()
        elif kind == "submit_field":
            action = state.submit_field(str(value or ""))
        elif kind == "answer_duplicate":
            action = state.answer_duplicate(bool(value))
        elif kind == "back":
            state.back()
        elif kind == "cancel":
            action = state.cancel()
        else:
            raise ValueError(f"unknown picker event {kind!r}")
        client = None
        if isinstance(action, (Select, Skip, Created)):
            client = apply_picker_result(action, session, self.store, self.s, self.meeting_map)
            self.events.publish("meeting.updated", session.to_json())
        done = isinstance(action, (Select, Skip, Created, DecideLater))
        if done:
            self.pickers.pop(session.id, None)
        return {**self._picker_view(session, state), "done": done, "client": client}

    @staticmethod
    def _picker_view(session: Session, state: PickerState) -> dict:
        return {"rows": [{"kind": r.kind, "label": r.label, "client": r.client, "via": r.via} for r in state.rows],
                "selected": state.selected, "mode": state.mode, "prompt": state.prompt, "hint": state.hint,
                "field_value": state.field_value, "query": state.query, "title": state.title,
                "headline": headline(session)[1]}

    # ---------------------------------------------------------------- recording settings (this computer's .env)
    def recording_settings(self) -> dict:
        return {"auto_record": self.s.auto_record, "hide_popup_from_sharing": self.s.hide_popup_from_sharing,
                "mic": self.mic_choice(), "recordings_folder": str(self.cfg.recordings_folder),
                "hotkeys": self.hotkeys(), "shortcut_failures": self.shortcut_failures}

    def hotkeys(self) -> dict:
        return {"popup": self.cfg.picker_hotkey, "screenshot": self.cfg.hotkey, "record": self.cfg.record_hotkey,
                "mark": self.cfg.mark_hotkey, "pause": self.cfg.gpu.hotkey}

    def shell_config(self) -> dict:
        return {"hotkeys": self.hotkeys(), "content_protected": self.s.hide_popup_from_sharing,
                "mic": self.mic_choice()}

    def change_settings(self, changes: dict) -> dict:
        """Validate everything first, then write each key to .env and apply it to the running engine."""
        writes: list[tuple[str, str]] = []
        for key in ("auto_record", "hide_popup_from_sharing"):
            if key in changes:
                if not isinstance(changes[key], bool):
                    raise ValueError(f"{key} must be true or false")
                writes.append((key.upper(), "true" if changes[key] else "false"))
        if "mic" in changes:
            mic = changes["mic"] if isinstance(changes["mic"], dict) else {}
            kind = mic.get("kind")
            if kind in ("default", "communications"):
                writes += [("MIC_DEVICE", kind), ("MIC_DEVICE_NAME", "")]
            elif kind == "device" and isinstance(mic.get("id"), str) and mic["id"].strip() and \
                    isinstance(mic.get("name"), str) and "\n" not in mic["id"] + mic["name"]:
                writes += [("MIC_DEVICE", mic["id"].strip()), ("MIC_DEVICE_NAME", mic["name"].strip())]
            else:
                raise ValueError("mic must be {kind: default|communications} or {kind: device, id, name}")
        for action, combo in (changes.get("hotkeys") or {}).items():
            if action not in HOTKEY_KEYS or not isinstance(combo, str) or (combo and not COMBO.fullmatch(combo)):
                raise ValueError(f"bad shortcut {action}: {combo!r}")
            writes.append((HOTKEY_KEYS[action], combo.lower()))
        for key, value in writes:
            set_env_key(self.env_file, key, value)
            os.environ[key] = value               # a restart of this engine reads the same values
        if writes:
            self._apply_env()
            self.link.send("config", self.shell_config())
            self.events.publish("settings.changed", self.recording_settings())
        return self.recording_settings()

    def _apply_env(self) -> None:
        env = os.environ.get
        self.s.auto_record = env("AUTO_RECORD", "false") == "true"
        self.s.hide_popup_from_sharing = env("HIDE_POPUP_FROM_SHARING", "true") != "false"
        self.s.mic_device, self.s.mic_device_name = env("MIC_DEVICE", "default"), env("MIC_DEVICE_NAME", "")
        for action, key in HOTKEY_KEYS.items():
            if key in os.environ:
                if action == "pause":
                    self.cfg.gpu = dataclasses.replace(self.cfg.gpu, hotkey=os.environ[key])
                else:
                    setattr(self.cfg, {"popup": "picker_hotkey", "screenshot": "hotkey"}.get(action, f"{action}_hotkey"),
                            os.environ[key])

    # ---------------------------------------------------------------- AI engine (Settings > AI engine)
    def hardware(self) -> dict:
        if self._hardware is None:
            self._hardware = hardware(self.catalogue)
        return self._hardware

    def builtin_model(self) -> dict:
        hw = self.hardware()
        chosen = os.getenv("BUILTIN_MODEL") or (hw["recommend"] if hw["recommend"] != "cloud" else "")
        return chat_entry(self.catalogue, chosen or self.catalogue["light_chat"])

    def ai_settings(self) -> dict:
        hw, engine = self.hardware(), engine_name(self.s)
        items = []
        for e in for_machine(self.catalogue, hw):
            files = e["files"]
            # optional: only search by meaning needs it, so "Download what the built-in AI needs" leaves it out
            optional = e["kind"] == "embedding" or (e["kind"] == "llama-server" and e["backend"] != hw["backend"])
            items.append({**self.downloads.status(e), "kind": e["kind"], "title": e["title"], "note": e.get("note"),
                          "size": Downloads.size(e), "licence": e["licence"], "optional": optional,
                          "checked": "sha256" if all(f.get("sha256") for f in files) else "size"})
        where = self.data_dir
        while not where.exists() and where.parent != where:
            where = where.parent
        address = self.s.openai_compat_base_url.removesuffix("/").removesuffix("/v1") \
            if engine in ("lmstudio", "ollama") else None
        key = self.s.anthropic_api_key or ""
        return {"engine": engine, "model": self.builtin_model()["id"], "address": address,
                "key_hint": f"...{key[-4:]}" if len(key) >= 8 else None, "hardware": hw, "items": items,
                "default_chat": self.catalogue["default_chat"], "light_chat": self.catalogue["light_chat"],
                "models_dir": str(self.data_dir / "models"), "free_bytes": shutil.disk_usage(where).free,
                "test": self.ai_test, "pending": self._ai_pending, "embedding": self.embedding}

    def change_ai(self, changes: dict) -> dict:
        """`{engine: builtin|lmstudio|ollama|cloud, model?, address?, api_key?}`, validated, then written to .env.
        Applied like a restart of the engine, once no meeting is being written."""
        engine = changes.get("engine")
        if engine not in ("builtin", "lmstudio", "ollama", "cloud"):
            raise ValueError("engine is builtin, lmstudio, ollama or cloud")
        writes: list[tuple[str, str]] = []
        if engine == "builtin":
            model = changes.get("model") or self.builtin_model()["id"]
            e = entry(self.catalogue, str(model))
            if e is None or e["kind"] != "chat":
                raise ValueError(f"unknown model {model!r}")
            writes += [("LLM_MODE", "local"), ("LOCAL_ENGINE", "builtin"), ("BUILTIN_MODEL", e["id"])]
        elif engine in ("lmstudio", "ollama"):
            address = str(changes.get("address") or ("http://localhost:1234" if engine == "lmstudio"
                                                       else "http://localhost:11434")).strip().rstrip("/")
            if not ADDRESS.fullmatch(address):
                raise ValueError("the address looks like http://localhost:1234")
            writes += [("LLM_MODE", "local"), ("LOCAL_ENGINE", "lmstudio" if engine == "lmstudio" else "server"),
                       ("LOCAL_BASE_URL", address.removesuffix("/v1") + "/v1")]
        else:
            key = changes.get("api_key")
            if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", key.strip())):
                raise ValueError("that does not look like an API key")
            if not key and not self.s.anthropic_api_key:
                raise ValueError("paste an API key first")
            writes.append(("LLM_MODE", "cloud"))
            if key:
                writes.append(("ANTHROPIC_API_KEY", key.strip()))
        for key, value in writes:
            set_env_key(self.env_file, key, value)
            os.environ[key] = value               # the restarted watch child and a later reload read the same
        self._ai_pending = True
        self.ai_test = None                       # a test of the old engine says nothing about the new one
        self._apply_ai()
        return self.ai_settings()

    def _apply_ai(self) -> bool:
        """Reload the settings, swap the model manager, restart watch; only while no meeting is being written."""
        from .config import load_settings
        from .gpu import activate, make_manager

        if _read_job(self.s.system / "ingest-status.json")["file"] or self.gpu_state.transcription_active():
            return False
        fresh = load_settings(self.env_file, vault_path=self.s.vault_path)
        for f in dataclasses.fields(fresh):
            setattr(self.s, f.name, getattr(fresh, f.name))
        if self.gpu is not None:
            old, new = self.gpu.manager, make_manager(self.s, root=self.data_dir, catalogue=self.catalogue)
            new.gate = old.gate
            self.gpu.manager = new
            activate(new)
            try:
                old.close()                       # e.g. stops the built-in server when LM Studio takes over
            except Exception:  # noqa: BLE001
                log.exception("Could not unload the previous engine's model")
        if self.watch is not None:
            self.watch.stop()                     # the status loop starts it again, with the new settings
        self._ai_pending = False
        log.info("AI engine is now %s.", engine_name(self.s))
        self.events.publish("settings.changed", {"ai": engine_name(self.s)})
        return True

    def download(self, entry_id: str, action: str) -> dict:
        if action not in ("start", "cancel"):
            raise ValueError("action is start or cancel")
        if action == "cancel":
            return self.downloads.cancel(entry_id)
        e = entry(self.catalogue, entry_id)
        build = embedding_server_entry(self.catalogue)
        if e is not None and e["kind"] == "embedding" and build and not installed(build, self.data_dir, self.catalogue):
            self.downloads.start(build["id"])         # the processor build the embedding server runs on
        return self.downloads.start(entry_id)

    def _download_progress(self, job: dict) -> None:
        self.events.publish("download.progress", job)
        if job.get("state") == "done":
            self._embed_wake.set()                    # the embedding model (or its server) may be complete now

    def start_ai_test(self) -> dict:
        """Test my setup, in the background; the result comes as the `ai.test` event."""
        with self._lock:
            if self.ai_test and self.ai_test.get("running"):
                return self.ai_test
            running = self.ai_test = {"running": True, "engine": engine_name(self.s)}

        def run() -> None:
            from .gpu import make_manager

            manager = make_manager(self.s, root=self.data_dir, catalogue=self.catalogue) \
                if engine_name(self.s) == "builtin" else None
            self.ai_test = setup_check(self.s, manager=manager)
            self.events.publish("ai.test", self.ai_test)

        self.events.publish("ai.test", running)
        threading.Thread(target=run, name="ai-test", daemon=True).start()
        return running

    def progress(self) -> dict:
        """What the pipeline is doing: watch's current file and step, and how much is waiting."""
        return {**_read_job(self.s.system / "ingest-status.json"),
                "inbox": len(_names(self.s.inbox)), "review": len(_names(self.s.review))}

    def poll_status(self) -> None:
        """Publish pipeline progress and the GPU pause state when either changed."""
        for event, data in (("pipeline.progress", self.progress()),
                            ("gpu.state", self.gpu_state.load().to_json())):
            if self._published.get(event) != data:
                self._published[event] = data
                self.events.publish(event, data)

    # ---------------------------------------------------------------- notes
    @property
    def index(self) -> NoteIndex:
        """The note index in the data folder, built on first use."""
        with self._index_lock:
            if self._index is None:
                index = NoteIndex(self.s.vault_path, self.data_dir / "index.sqlite")
                started = time.monotonic()
                changed = index.refresh()
                log.info("Note index: %d notes updated in %.1f s%s.", len(changed), time.monotonic() - started,
                         "" if index.fts else " (no FTS5: plain search)")
                self._index = index
            return self._index

    def _watch_notes(self) -> None:
        try:
            index = self.index
            def changed(paths: list[str]) -> None:
                self.events.publish("notes.changed", {"paths": paths[:200], "count": len(paths)})
                self._embed_wake.set()

            index.watch(changed)
            index.refresh()                   # whatever changed between the first build and the watcher's start
        except Exception:  # noqa: BLE001
            log.exception("The note index could not start; notes are not kept current")

    # ---------------------------------------------------------------- search by meaning
    def _set_embedding(self, state: str, message: str | None = None, model: str | None = None) -> None:
        counts = self.index.embedded_count(model) if model else {"done": 0, "total": 0}
        data = {"state": state, **counts, "message": message}
        if data != self.embedding:
            self.embedding = data
            self.events.publish("embeddings.progress", data)

    def _embedding_client(self) -> embeddings.EmbeddingClient:
        """The client of the embedding server, started when it is not running."""
        if os.getenv("EMBEDDING_BASE_URL"):
            client = embeddings.running_client(self.data_dir, self.catalogue)
        else:
            client = self.embed_server.client() or self.embed_server.client(self.embed_server.start())
        if client is None:
            raise embeddings.EmbeddingUnavailable("The embedding server did not start.")
        self.embedder = client
        return client

    def embed_pending(self) -> None:
        """Embed every note whose current version has no vectors yet, newest first."""
        model = embeddings.model_id(self.data_dir, self.catalogue)
        if model is None:
            self.embedder = None
            self._set_embedding("off")
            return
        if self.embedder is None:
            self._set_embedding("starting", model=model)
        client = self._embedding_client()
        while not self._stop.is_set():
            if self.gpu_state.transcription_active():
                self._set_embedding("waiting", "Waits while a recording is transcribed.", model)
                self._stop.wait(EMBED_RETRY_SECONDS)
                continue
            notes = self.index.to_embed(model, 8)
            if not notes:
                break
            self._set_embedding("indexing", model=model)
            for note in notes:
                pieces = embeddings.chunk_note(note["title"], note["body"] or "")
                vectors = client.embed([embeddings.embed_input(note["title"], h, t) for h, t in pieces])
                self.index.put_vectors(note, model, pieces, vectors)
        self._set_embedding("ready", model=model)

    def _embed_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.embed_pending()
            except Exception as e:  # noqa: BLE001  the server may be gone; shown in the app, tried again later
                log.warning("Search by meaning: %s", e)
                self.embedder = None
                self._set_embedding("failed", str(e) or e.__class__.__name__)
                self._stop.wait(EMBED_RETRY_SECONDS)
                continue
            self._embed_wake.wait(EMBED_IDLE_SECONDS)
            self._embed_wake.clear()

    def semantic_search(self, query: str, limit: int, types: tuple[str, ...] | None) -> list[dict] | None:
        """Notes by meaning for the search field; None when search by meaning is not running."""
        model = embeddings.model_id(self.data_dir, self.catalogue)
        client = self.embedder
        if model is None or client is None or not query.strip():
            return None
        return self.index.semantic(client.embed([query])[0], model, limit, query=query, types=types)

    # ---------------------------------------------------------------- Connect AI tools
    def connections(self, check: bool = True) -> dict:
        """Settings > Connect AI tools. The server test (the tools' own command, once) is cached for a minute."""
        want, paths = connect.server_entry(), self.connect_paths
        if check and any(connect.status(t, want, path=paths.get(t))["state"] == "connected" for t in connect.TOOLS):
            if self._server_check is None or time.monotonic() - self._server_check[0] > 60:
                self._server_check = (time.monotonic(), connect.check_server(want))
            result = self._server_check[1]
        else:
            result = "unknown"
        return connect.overview(want, paths=paths, check=result)

    def change_connection(self, tool: str, action: str, name: str | None = None) -> dict:
        if tool not in connect.TOOLS:
            raise KeyError(tool)
        want, path = connect.server_entry(), self.connect_paths.get(tool)
        if action == "connect":
            row = connect.status(tool, want, path=path)
            if not row["connectable"]:
                raise connect.ConnectError(row["reason"])
            done = connect.connect(tool, want, path=path)
        elif action == "disconnect":
            done = connect.disconnect(tool, path=path)
        elif action == "remove_old" and name:
            done = connect.remove_old(tool, str(name), path=path)
        else:
            raise ValueError("action is connect, disconnect or remove_old (with name)")
        log.info("Connect AI tools: %s %s (%s; backup %s).", action, tool, done["path"], done["backup"])
        self._server_check = None
        return {**self.connections(), "done": {"tool": tool, "action": action, **done}}

    # ---------------------------------------------------------------- threads
    def start(self, lister) -> None:
        """Start detection (`lister.list()` gives the open windows), the GPU guard, the recording queue,
        the status poller and the watch child."""
        from .gpu import GpuService

        self.companion.load_recent()
        self.companion.shots.prime()
        self.gpu = GpuService(self.s, self.cfg.gpu, self.vault, is_companion=True)
        self.gpu.start(tray=False)
        if self.s.transcript_source == "whisper":
            self.recordings = self._recording_queue()
        for name, target in (("engine-detect", lambda: self._detect(lister)), ("engine-status", self._status),
                             ("engine-notes", self._watch_notes), ("engine-embed", self._embed_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._embed_wake.set()
        self.downloads.stop()
        self.events.close()
        for thread in self._threads:
            thread.join(timeout=10)
        if self.watch is not None:
            self.watch.stop()
        if self.gpu is not None:
            self.gpu.stop()
        if self.recordings is not None:
            self.recordings.stop()
        self.companion.stop()
        if not os.getenv("EMBEDDING_BASE_URL"):
            self.embed_server.stop()
        if self._index is not None:
            self._index.close()

    def _recording_queue(self):
        from .sessions import overlaps_skipped
        from .transcribe import RecordingQueue

        def skip_recording(when: dt.datetime) -> bool:
            days = [(when.date() + dt.timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)]
            return overlaps_skipped(self.store.load(days), when)

        queue = RecordingQueue(self.cfg.recordings_folder, self.s.inbox, self.s.system / "whisper-sources.json",
                               self.vault, self.s, self.cfg.audio_settle_seconds,
                               clients=lambda: _client_names(self.s), should_skip=skip_recording)
        queue.gpu = self.gpu
        queue.start()
        return queue

    def _detect(self, lister) -> None:
        next_lm = time.time() + LM_RETRY_SECONDS
        while not self._stop.is_set():
            now = time.time()
            if not self.gpu.server_checked and now >= next_lm:
                if not self.gpu.retry_startup_cleanup():
                    next_lm = now + LM_RETRY_SECONDS
            try:
                self.tick(now, lister.list())
            except Exception:  # noqa: BLE001
                log.exception("Meeting detection failed")
            try:
                self.companion.shots.poll()
            except Exception:  # noqa: BLE001
                log.exception("Screenshot scan failed")
            if self.recordings is not None:
                try:
                    self.recordings.poll()
                    self.gpu.set_counts(
                        recordings=len(self.recordings._jobs) + (1 if self.recordings.inflight else 0))
                except Exception:  # noqa: BLE001
                    log.exception("Recording scan failed")
            self._stop.wait(self.cfg.poll_seconds)

    def _status(self) -> None:
        while not self._stop.is_set():
            try:
                if self._ai_pending:
                    self._apply_ai()
                if self.watch is not None:
                    self.watch.poll(time.time())
                self.poll_status()
            except Exception:  # noqa: BLE001
                log.exception("Status check failed")
            self._stop.wait(STATUS_POLL_SECONDS)


def open_engine(s: Settings, cfg: CompanionConfig, watch_argv: list[str], link: ShellLink | None = None,
                env_file: Path | None = None) -> Engine:
    """The engine with this computer's probes: what `run_companion` builds, without its window and tray."""
    from .desktop import AudioProbe
    from .meeting_signals import MicWatcher
    from .watcher_meetings import GameProbe

    engine = None
    games = GameProbe(cfg.gpu, window_pids=lambda: {w.pid for w in engine.companion.last_windows} if engine else None)
    engine = Engine(s, cfg, audio=AudioProbe().playing, mic=MicWatcher(cfg.mic) if cfg.mic.enabled else None,
                    gaming=games.running, watch=WatchChild(watch_argv), link=link, env_file=env_file)
    return engine
