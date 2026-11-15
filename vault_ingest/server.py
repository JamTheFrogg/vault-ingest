"""`vault-ingest serve`: the local HTTP API and event stream the desktop app talks to.

Bound to 127.0.0.1 on a free port. Every request needs the token: VAULT_INGEST_TOKEN when the desktop app
starts this process, else one made here. When it accepts requests the process prints one line,
`@@ ready {"port": N}` (with `"token"` when it made the token itself); other lines on stdout are log text.
"""
from __future__ import annotations

import dataclasses
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import threading
import time

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .app import Engine, open_engine
from .cli import _names, _pid_alive, _read_job, _read_pid
from .clients import load_clients, rank_clients
from .config import PROJECT_DIR, home, load_settings

log = logging.getLogger("vault_ingest")
TOKEN_ENV = "VAULT_INGEST_TOKEN"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
CALL_FIELDS = ("client", "date", "title", "call_note", "chunks", "processed_at", "generated_by", "screenshots")


class TokenAuth:
    """401 unless the request carries the token: `Authorization: Bearer <token>`, or `?token=` on a GET
    (EventSource, <img> and a browser's address bar cannot set headers)."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not given and request.method == "GET":
                given = request.query_params.get("token", "")
            if not hmac.compare_digest(given.encode(), self.token.encode()):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _read_log(path) -> dict:
    """ingest-log.json, read-only (`vault.Registry` writes a backup copy when the file is corrupt)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def make_app(engine: Engine, token: str) -> Starlette:
    s = engine.s

    def status(request):
        watcher = _read_pid(s.system / "ingest.pid")
        return JSONResponse({
            "vault": str(s.vault_path), "mode": s.effective_mode, "model": s.llm_model,
            "inbox": _names(s.inbox), "review": _names(s.review),
            "job": _read_job(s.system / "ingest-status.json"),
            "gpu": engine.gpu_state.load().to_json(),
            "watcher": watcher if watcher and _pid_alive(watcher) else None,
        })

    def meetings(request):
        # shortcut: every call and session on each request, fine for a few thousand; page it with the note index
        sessions = engine.store.load()
        ids = {engine.vault.rel(x.path): x.id for x in sessions if x.path}
        calls = {}
        for rec in _read_log(s.system / "ingest-log.json").values():     # an audio call is logged under two hashes
            if isinstance(rec, dict) and rec.get("call_note"):
                calls[rec["call_note"]] = {**{k: rec.get(k) for k in CALL_FIELDS},
                                           "session_id": ids.get(rec.get("meeting_session"))}
        newest = sorted(calls.values(), key=lambda c: (str(c["date"]), str(c["processed_at"])), reverse=True)
        return JSONResponse({"calls": newest, "sessions": [x.to_json() for x in reversed(sessions)],
                             "waiting": _names(s.inbox)})

    def current_session(request):
        live = engine.companion.latest_session()
        return JSONResponse(live.to_json() if live else None)

    def session(request):
        sid = request.path_params["id"]
        if not SESSION_ID.fullmatch(sid):
            return JSONResponse({"error": "bad session id"}, status_code=400)
        found = engine.store.find(sid)
        if found is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        return JSONResponse(found.to_json())

    def clients(request):
        records = load_clients(s.clients)
        query = request.query_params.get("q", "").strip()
        rows = rank_clients(records, query) if query else records
        return JSONResponse({"clients": [dataclasses.asdict(r) for r in rows]})

    async def events(request):
        async def stream():
            queue = engine.events.subscribe()
            try:
                while (item := await queue.get()) is not None:
                    event, data = item
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            finally:
                engine.events.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return Starlette(
        routes=[Route("/status", status), Route("/meetings", meetings),
                Route("/sessions/current", current_session), Route("/sessions/{id}", session),
                Route("/clients", clients), Route("/events", events)],
        # no CORS headers: only the token holder on this computer may read the API
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"]),
                    Middleware(TokenAuth, token=token)])


def serve(engine: Engine, lister, token: str, *, show_token: bool, parent_pid: int | None = None) -> int:
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    # no access log: a token passed as ?token= would end up in it
    server = uvicorn.Server(uvicorn.Config(make_app(engine, token), log_config=None, access_log=False))
    api = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="api", daemon=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def read_stdin() -> None:
        for line in sys.stdin:
            if line.strip() == "quit":
                break
        stop.set()                                # "quit", or the pipe closed because the app is gone

    api.start()
    try:
        engine.start(lister)
        while api.is_alive() and not server.started:
            time.sleep(0.05)
        if not server.started:
            log.error("The API could not start.")
            return 1
        ready = {"port": port, "token": token} if show_token else {"port": port}
        print(f"@@ ready {json.dumps(ready)}", flush=True)      # the token goes to stdout only, never to a log file
        log.info("API on http://127.0.0.1:%d. Ctrl+C to stop.", port)
        if parent_pid and sys.stdin is not None:
            # only under the desktop app: from a terminal or a background task, a closed stdin must not stop it
            threading.Thread(target=read_stdin, name="shell-link", daemon=True).start()
        while api.is_alive() and not stop.wait(1.0):
            if parent_pid and not _pid_alive(parent_pid):
                log.info("The desktop app (pid %s) is gone; stopping.", parent_pid)
                break
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()                             # also ends the open /events streams, so the server can finish
        server.should_exit = True
        api.join(timeout=10)
    return 0


def main_serve(args) -> int:
    from .cli import setup_logging
    from .watcher_meetings import DEFAULT_CONFIG, WindowLister, acquire_lock, load_config

    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    setup_logging(s.system / "companion.log", args.verbose)
    if not DEFAULT_CONFIG.is_file() and home() != PROJECT_DIR:
        home().mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT_DIR / "companion.toml", DEFAULT_CONFIG)      # first run in a new settings folder
    cfg = load_config()
    watch_argv = [sys.executable, "-m", "vault_ingest"]
    if args.env:
        watch_argv += ["--env", args.env]
    if getattr(args, "vault", None):
        watch_argv += ["--vault", args.vault]
    if args.verbose:
        watch_argv.append("-v")
    engine = open_engine(s, cfg, watch_argv + ["watch", "--no-tray"])
    if not acquire_lock(engine.vault, s.system / "companion.lock"):
        log.error("The meeting companion or another `serve` is already running (see %s).",
                  s.system / "companion.lock")
        return 1
    token = os.environ.get(TOKEN_ENV, "").strip()
    return serve(engine, WindowLister(), token or secrets.token_urlsafe(32), show_token=not token,
                 parent_pid=args.parent_pid)
