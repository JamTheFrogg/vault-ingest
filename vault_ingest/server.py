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
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .app import Engine, ShellLink, open_engine
from .cli import _names, _pid_alive, _read_job, _read_pid
from .clients import load_clients, rank_clients
from .picker import Select, Skip, apply_picker_result
from .config import DEFAULT_ENV_FILE, PROJECT_DIR, home, load_settings, set_env_key
from .connect import ConnectError
from .index import DEFAULT_GRAPH_TYPES, NOTE_TYPES
from .vault import get_frontmatter, split_frontmatter

log = logging.getLogger("vault_ingest")
TOKEN_ENV = "VAULT_INGEST_TOKEN"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
QUERY_TOKEN_PATHS = {"/events"}
# The app's page: tauri://localhost (macOS, Linux), http://tauri.localhost (Windows), Vite on 1420 in development
APP_ORIGINS = ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost", "http://localhost:1420"]
NOTE_KINDS = (*NOTE_TYPES, "other")
CALL_FIELDS = ("client", "date", "title", "call_note", "chunks", "processed_at", "generated_by", "screenshots")


class TokenAuth:
    """401 unless the request carries the token: `Authorization: Bearer <token>`, or `?token=` on GET /events
    only (EventSource cannot set headers; a token in any other URL would leak into history and logs)."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not given and request.method == "GET" and scope["path"] in QUERY_TOKEN_PATHS:
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
                             "waiting": _names(s.inbox), "recordings": engine.recording_rows(),
                             "recording": engine.recording})

    def current_session(request):
        live = engine.companion.latest_session()
        return JSONResponse(live.to_json() if live else None)

    async def session(request):
        if request.method == "PATCH":
            return await change_session(request)
        found, error = lookup(request.path_params["id"])
        return error or JSONResponse(found.to_json())

    def lookup(sid: str):
        if not SESSION_ID.fullmatch(sid):
            return None, JSONResponse({"error": "bad session id"}, status_code=400)
        found = engine.find_session(sid)
        if found is None:
            return None, JSONResponse({"error": "no such session"}, status_code=404)
        return found, None

    async def body(request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ValueError("the body must be a JSON object")
        return data

    async def change_session(request):
        """`{"client": name, "title": text}` or `{"skip": true}`: the picker's decisions without the picker."""
        found, error = lookup(request.path_params["id"])
        if error:
            return error
        try:
            data = await body(request)
            title = str(data.get("title") or found.call_title or "")
            if data.get("skip") is True:
                action = Skip(title)
            elif isinstance(data.get("client"), str) and data["client"].strip():
                action = Select(data["client"].strip(), title)
            else:
                raise ValueError("give a client or skip: true")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        apply_picker_result(action, found, engine.store, s, engine.meeting_map)
        engine.events.publish("meeting.updated", found.to_json())
        return JSONResponse(found.to_json())

    def record_session(request):
        found, error = lookup(request.path_params["id"])
        return error or JSONResponse(engine.record(found))

    def record_start(request):
        return JSONResponse(engine.record())

    def record_stop(request):
        return JSONResponse(engine.stop_recording())

    def record_mark(request):
        return JSONResponse(engine.mark())

    def recording(request):
        return JSONResponse(engine.recording)

    def popup(request):
        return JSONResponse(engine.popup())

    async def screenshot(request):
        try:
            kind = (await body(request)).get("kind", "region")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if kind not in ("region", "screen"):
            return JSONResponse({"error": "kind is region or screen"}, status_code=400)
        await run_in_threadpool(engine.screenshot, kind)
        return JSONResponse({"ok": True})

    async def picker_open(request):
        try:
            found, error = lookup(str((await body(request)).get("session") or ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return error or JSONResponse(engine.picker_open(found))

    async def picker_event(request):
        try:
            data = await body(request)
            found, error = lookup(str(data.get("session") or ""))
            if error:
                return error
            return JSONResponse(engine.picker_event(found, str(data.get("type")), data.get("value")))
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    async def recording_settings(request):
        if request.method == "GET":
            return JSONResponse(engine.recording_settings())
        try:
            return JSONResponse(engine.change_settings(await body(request)))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    async def ai_settings(request):
        if request.method == "GET":
            return JSONResponse(await run_in_threadpool(engine.ai_settings))
        try:
            return JSONResponse(await run_in_threadpool(engine.change_ai, await body(request)))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    async def ai_download(request):
        try:
            action = str((await body(request)).get("action") or "start")
            return JSONResponse(engine.download(request.path_params["id"], action))
        except KeyError:
            return JSONResponse({"error": "no such download"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    def ai_test(request):
        return JSONResponse(engine.start_ai_test())

    def clients(request):
        """Client folders with aliases and contacts, and from the note index: meetings, newest meeting, open items."""
        records = load_clients(s.clients)
        query = request.query_params.get("q", "").strip()
        rows = rank_clients(records, query) if query else records
        stats = engine.index.client_stats()
        empty = {"meetings": 0, "last_meeting": None, "open_items": 0, "profile": None}
        return JSONResponse({"clients": [{**dataclasses.asdict(r), **stats.get(r.name, empty)} for r in rows]})

    def vault_note(raw: str):
        """(vault-relative path, None) for a .md file inside the vault, else (None, the error response)."""
        try:
            path = (engine.vault.root / raw).resolve()
        except (OSError, ValueError):                         # e.g. a NUL byte
            return None, JSONResponse({"error": "bad path"}, status_code=400)
        if not raw or path.suffix.lower() != ".md" or engine.vault.root not in path.parents:
            return None, JSONResponse({"error": "only .md files inside the vault"}, status_code=400)
        return path, None

    def note(request):
        """One Markdown note of the vault, read-only: parsed frontmatter, the body and its resolved links."""
        path, error = vault_note(request.query_params.get("path", ""))
        if error:
            return error
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return JSONResponse({"error": "no such note"}, status_code=404)
        except (OSError, UnicodeDecodeError):
            return JSONResponse({"error": "the note cannot be read"}, status_code=422)
        rel = engine.vault.rel(path)
        data = {"path": rel, "frontmatter": get_frontmatter(text), "body": split_frontmatter(text)[1],
                "links": engine.index.links(text, rel)}
        # YAML gives dates as date objects; str() keeps them as written (2026-09-30)
        return Response(json.dumps(data, ensure_ascii=False, default=str), media_type="application/json")

    def backlinks(request):
        path, error = vault_note(request.query_params.get("path", ""))
        return error or JSONResponse({"backlinks": engine.index.backlinks(engine.vault.rel(path))})

    def listed(request, name: str, allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
        values = tuple(v.strip() for v in request.query_params.get(name, "").split(",") if v.strip())
        if allowed is not None and any(v not in allowed for v in values):
            raise ValueError(f"{name} takes {', '.join(allowed)}")
        return values

    def search(request):
        """Ranked notes for `q`, each with a snippet as [text, is_hit] pieces. `types=call,client,...` narrows it."""
        try:
            limit = int(request.query_params.get("limit", "20"))
            types = listed(request, "types", NOTE_KINDS)
            if not 1 <= limit <= 200:
                raise ValueError("limit is 1 to 200")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        q = request.query_params.get("q", "")
        if request.query_params.get("semantic") == "1":
            try:
                found = engine.semantic_search(q, limit, types or None)
            except (ValueError, OSError, RuntimeError) as e:          # the embedding server went away
                log.warning("Search by meaning failed: %s", e)
                found = None
            if found is None:
                return JSONResponse({"error": "search by meaning is not available", "semantic": engine.embedding},
                                    status_code=409)
            return JSONResponse({"results": found, "fts": engine.index.fts, "semantic": engine.embedding})
        return JSONResponse({"results": engine.index.search(q, limit, types or None), "fts": engine.index.fts,
                             "semantic": engine.embedding})

    async def connections(request):
        return JSONResponse(await run_in_threadpool(engine.connections))

    async def change_connection(request):
        try:
            data = await body(request)
            return JSONResponse(await run_in_threadpool(engine.change_connection, request.path_params["tool"],
                                                        str(data.get("action") or ""), data.get("name")))
        except KeyError:
            return JSONResponse({"error": "no such tool"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except (ConnectError, OSError) as e:
            return JSONResponse({"error": str(e)}, status_code=409)

    def graph(request):
        """Notes and links for the graph. Default: clients, meetings, other notes; never _System or raw files."""
        try:
            types = listed(request, "types", NOTE_KINDS) or DEFAULT_GRAPH_TYPES
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(engine.index.graph(types, listed(request, "client"),
                                               request.query_params.get("missing") == "1"))

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
                Route("/sessions/current", current_session), Route("/sessions/{id}", session, methods=["GET", "PATCH"]),
                Route("/sessions/{id}/record", record_session, methods=["POST"]),
                Route("/sessions/{id}/screenshot", screenshot, methods=["POST"]),
                Route("/recording", recording), Route("/recording/start", record_start, methods=["POST"]),
                Route("/recording/stop", record_stop, methods=["POST"]),
                Route("/recording/mark", record_mark, methods=["POST"]), Route("/popup", popup),
                Route("/picker/open", picker_open, methods=["POST"]),
                Route("/picker/event", picker_event, methods=["POST"]),
                Route("/settings/recording", recording_settings, methods=["GET", "PATCH"]),
                Route("/settings/ai", ai_settings, methods=["GET", "PATCH"]),
                Route("/settings/ai/downloads/{id}", ai_download, methods=["POST"]),
                Route("/settings/ai/test", ai_test, methods=["POST"]),
                Route("/clients", clients), Route("/notes", note), Route("/backlinks", backlinks),
                Route("/search", search), Route("/graph", graph), Route("/events", events),
                Route("/connect", connections), Route("/connect/{tool}", change_connection, methods=["POST"])],
        # CORS only for the desktop app's own pages; the token is still required. The preflight carries no token,
        # so CORS sits in front of TokenAuth.
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"]),
                    Middleware(CORSMiddleware, allow_origins=APP_ORIGINS, allow_methods=["GET", "POST", "PATCH"],
                               allow_headers=["Authorization", "Content-Type"]),
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
            engine.on_shell(line)
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
        engine.link.send("config", engine.shell_config())        # shortcuts, popup protection, the microphone
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
        # first run in a new settings folder: a new install records into its own folder; an imported install
        # brings its companion.toml (and with it the Meetily folder) and never reaches this line
        home().mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT_DIR / "companion.toml", DEFAULT_CONFIG)
        if not os.getenv("RECORDINGS_FOLDER"):
            from .desktop import default_recordings_folder

            folder = str(default_recordings_folder())
            set_env_key(args.env or DEFAULT_ENV_FILE, "RECORDINGS_FOLDER", folder)
            os.environ["RECORDINGS_FOLDER"] = folder
    cfg = load_config()
    watch_argv = [sys.executable, "-m", "vault_ingest"]
    if args.env:
        watch_argv += ["--env", args.env]
    if getattr(args, "vault", None):
        watch_argv += ["--vault", args.vault]
    if args.verbose:
        watch_argv.append("-v")
    under_app = bool(args.parent_pid)
    link = ShellLink(lambda line: print(line, flush=True)) if under_app else None
    engine = open_engine(s, cfg, watch_argv + ["watch", "--no-tray"], link=link, env_file=args.env or DEFAULT_ENV_FILE)
    if not acquire_lock(engine.vault, s.system / "companion.lock"):
        log.error("The meeting companion or another `serve` is already running (see %s).",
                  s.system / "companion.lock")
        return 1
    token = os.environ.get(TOKEN_ENV, "").strip()
    return serve(engine, WindowLister(), token or secrets.token_urlsafe(32), show_token=not token,
                 parent_pid=args.parent_pid)
