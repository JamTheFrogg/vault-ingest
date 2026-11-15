"""The engine behind `vault-ingest serve`: meeting detection without a window or tray, the `watch` child,
and the events the UI listens to. The window, tray and hotkeys belong to the desktop app."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import subprocess
import threading
import time
from typing import Callable

from .cli import _client_names, _names, _read_job
from .config import Settings
from .gpu import STATE_NAME, GpuStateStore
from .sessions import SessionStore
from .vault import Vault
from .watcher_meetings import LM_RETRY_SECONDS, Companion, CompanionConfig, Ended

log = logging.getLogger("vault_ingest")
WATCH_RESTART_SECONDS = 10
STATUS_POLL_SECONDS = 1


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
                 mic=None, gaming=None, watch: WatchChild | None = None):
        self.s, self.cfg = s, cfg
        self.vault = Vault(s.vault_path, (s.inbox_dir, s.system_dir))
        self.store = SessionStore(s.system / "meetings", self.vault)
        self.gpu_state = GpuStateStore(s.system / STATE_NAME)
        self.events = Events()
        self.watch = watch
        self.companion = Companion(
            s, cfg, self.vault, self.store, audio=audio, mic=mic, gaming=gaming,
            on_started=lambda session: self.events.publish("meeting.started", session.to_json()),
            on_retitle=lambda session: self.events.publish("meeting.updated", session.to_json()))
        self.gpu = None
        self.recordings = None
        self._published: dict[str, dict] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---------------------------------------------------------------- one round each
    def tick(self, now: float, windows: list) -> None:
        live = self.companion.session
        for ev in self.companion.tick(now, windows):
            if isinstance(ev, Ended) and live is not None:
                self.events.publish("meeting.ended", live.to_json())

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
        for name, target in (("engine-detect", lambda: self._detect(lister)), ("engine-status", self._status)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
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
                if self.watch is not None:
                    self.watch.poll(time.time())
                self.poll_status()
            except Exception:  # noqa: BLE001
                log.exception("Status check failed")
            self._stop.wait(STATUS_POLL_SECONDS)


def open_engine(s: Settings, cfg: CompanionConfig, watch_argv: list[str]) -> Engine:
    """The engine with this computer's probes: what `run_companion` builds, without its window and tray."""
    from .desktop import AudioProbe
    from .meeting_signals import MicWatcher
    from .watcher_meetings import GameProbe

    engine = None
    games = GameProbe(cfg.gpu, window_pids=lambda: {w.pid for w in engine.companion.last_windows} if engine else None)
    engine = Engine(s, cfg, audio=AudioProbe().playing, mic=MicWatcher(cfg.mic) if cfg.mic.enabled else None,
                    gaming=games.running, watch=WatchChild(watch_argv))
    return engine
