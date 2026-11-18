"""The built-in engine: llama.cpp's llama-server as a second process, in place of LM Studio's `lms`.

`LlamaServerManager` overrides only the seams of `gpu.ModelManager` that call `lms`; loading, unloading on a
pause, the exit hook and the VRAM logs are inherited. `<home>/state/llama-server.json` ({pid, port, key, model})
makes the server one shared instance for `serve` and its `watch` child, as LM Studio is today.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable

import httpx

from . import models
from .config import home
from .gpu import ModelManager, parse_other_vram_mb

log = logging.getLogger("vault_ingest")
STATE_FILE = "llama-server.json"
LOAD_TIMEOUT = 600                     # a 7 GB model from a slow disk; the log says what it is doing meanwhile
NOT_DOWNLOADED = "The built-in AI is not downloaded yet. Open Settings > AI engine."
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def offloaded(log_text: str) -> tuple[int, int] | None:
    """`load_tensors: offloaded 49/49 layers to GPU`: how much of the model is on the card (the last load)."""
    found = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", log_text or "")
    return (int(found[-1][0]), int(found[-1][1])) if found else None


def _is_llama(pid: int) -> bool:
    """The pid is still our llama-server, not a new process that got the same number."""
    try:
        import psutil

        proc = psutil.Process(int(pid))
        return any("llama-server" in part for part in [proc.name(), *proc.cmdline()])
    except Exception:  # noqa: BLE001  gone, no access, or psutil missing
        return False


class LlamaServerManager(ModelManager):
    STATE, LOG = STATE_FILE, "llama-server.log"

    def __init__(self, settings, *, root: Path | None = None, catalogue: dict | None = None,
                 popen: Callable = subprocess.Popen, backend: str | None = None, **kw):
        super().__init__(settings, **kw)
        self.root = Path(root or home())
        self.cat = catalogue or models.load_catalogue()
        self._popen = popen
        self.backend = backend
        self.state_path = self.root / "state" / self.STATE
        self.log_path = self.root / "logs" / self.LOG

    @property
    def managing(self) -> bool:
        return self.s.effective_mode == "local"

    @property
    def chat(self) -> dict:
        return models.chat_entry(self.cat, self.s.llm_model)

    # ---- the shared server
    def info(self) -> dict | None:
        """The running server from the state file, or None."""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and _is_llama(data.get("pid", 0)) else None

    def health(self, info: dict | None) -> str | None:
        """ok, loading (503 while the model loads), or None when nothing answers."""
        if not info:
            return None
        try:
            r = httpx.get(f"http://127.0.0.1:{info['port']}/health", timeout=3,
                          headers={"Authorization": f"Bearer {info.get('key', '')}"})
        except httpx.HTTPError:
            return None
        return "ok" if r.status_code == 200 else "loading" if r.status_code == 503 else None

    def healthy(self, info: dict | None) -> bool:
        return self.health(info) == "ok"

    def _wait(self, info: dict, proc=None) -> bool:
        """Until the server answers ok; False when it died, hung past LOAD_TIMEOUT, or the GPU was taken back."""
        deadline = time.time() + LOAD_TIMEOUT
        while (state := self.health(info)) != "ok":
            gone = proc.poll() is not None if proc is not None else not _is_llama(info["pid"]) or state is None
            if gone or time.time() > deadline or (self.gate is not None and self.gate.cancel.is_set()):
                return False
            time.sleep(0.5)
        return True

    def _use(self, info: dict) -> None:
        # LocalStudioLLM reads both on every request, so a client made before the server started finds it
        self.s.openai_compat_base_url = f"http://127.0.0.1:{info['port']}/v1"
        self.s.openai_compat_api_key = info["key"]

    def start(self) -> dict:
        info = self.info()
        if info and self._wait(info):                    # running, or still loading for the other process
            self._use(info)
            return info
        if info:
            self.stop()                                   # alive but not answering: a hang or a load that failed
        port, key = free_port(), secrets.token_urlsafe(24)
        server, argv, alias = self._argv(port)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        log.info("Starting llama-server (%s) on port %d.", alias, port)
        with open(self.log_path, "w", encoding="utf-8", errors="replace") as out:
            # the key as LLAMA_API_KEY (same as --api-key), so it is not in the process list
            proc = self._popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                               cwd=str(server.parent), env={**os.environ, "LLAMA_API_KEY": key},
                               creationflags=_NO_WINDOW)
        info = {"pid": proc.pid, "port": port, "key": key, "model": alias}
        self.state_path.write_text(json.dumps(info), encoding="utf-8")
        if not self._wait(info, proc):
            held = self.gate is not None and self.gate.cancel.is_set()
            tail = " ".join(self.log_tail().split())[-400:]
            self.stop(proc)
            if held:
                from .gpu import GpuInterrupted
                raise GpuInterrupted("GPU hold")
            from .llm import LLMUnavailable
            raise LLMUnavailable(f"llama-server did not start: {tail or 'no output'}")
        self._use(info)
        log.info("llama-server is ready (pid %d).", proc.pid)
        return info

    def _argv(self, port: int) -> tuple[Path, list[str], str]:
        """(server binary, command line, model alias) of the chat server."""
        backend = self.backend or models.hardware(self.cat)["backend"]
        server = models.server_binary(self.cat, self.root, backend)
        files = models.model_files(self.chat, self.root, self.cat)
        if server is None or files is None:
            from .llm import LLMUnavailable
            raise LLMUnavailable(NOT_DOWNLOADED)
        argv = [str(server), "-m", str(files["model"])]
        if "mmproj" in files:
            argv += ["--mmproj", str(files["mmproj"])]
        # -ngl auto: as many layers on the card as fit; -np 1: one slot, as `lms load --parallel 1` did
        argv += ["-c", str(int(getattr(self.s, "local_context", 8192) or 8192)), "-ngl", "auto", "-np", "1",
                 "--host", "127.0.0.1", "--port", str(port), "--alias", self.chat["alias"]]
        log.info("Chat model %s on the %s build.", self.chat["alias"], backend)
        return server, argv, self.chat["alias"]

    def stop(self, proc=None) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        pid = getattr(proc, "pid", None) or data.get("pid")
        if pid and (proc is not None or _is_llama(pid)):
            log.info("Stopping llama-server (pid %s).", pid)
            try:
                import psutil

                target = psutil.Process(int(pid))
                target.terminate()
                try:
                    target.wait(15)
                except psutil.TimeoutExpired:
                    target.kill()
            except Exception:  # noqa: BLE001  already gone
                pass
            if proc is not None:
                try:
                    proc.wait(5)                          # reap our own child
                except Exception:  # noqa: BLE001
                    pass
        self.state_path.unlink(missing_ok=True)

    def log_tail(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return ""

    def offload(self) -> tuple[int, int] | None:
        try:
            return offloaded(self.log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return None

    # ---- the seams of ModelManager that used `lms`
    def _lms(self, *args: str, timeout: float = 180):
        if args[0] == "load":
            self.start()
        elif args[0] == "unload":
            self.stop()

    def list_models(self) -> list[dict]:
        if self._models_fn is not None:
            return list(self._models_fn())
        info = self.info()
        state = "loaded" if info and self.healthy(info) else "not-loaded"
        return [{"id": self.chat["alias"], "type": "llm", "state": state}]

    def ps(self) -> list[dict]:
        return []

    def server_reachable(self, timeout: float = 3) -> bool:
        return True

    def ensure_server(self) -> None:
        return None

    def ours_loaded(self) -> bool:
        # the server is ours whichever of the two engine processes started it
        return self.whisper_active or self.info() is not None

    def other_vram_mb(self) -> int | None:
        text = self._nvidia_text(["--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"])
        if text is None:
            return None
        info = self.info()
        return parse_other_vram_mb(text, {os.getpid(), *([int(info["pid"])] if info else [])})


def setup_check(s, *, root: Path | None = None, catalogue: dict | None = None, manager=None) -> dict:
    """"Test my setup": load the model, one short answer, speed and whether it fits, then unload. For the other
    engines the same short answer from whatever they serve. Never raises; the result says what went wrong."""
    from .gpu import GpuInterrupted, fits_in_dedicated, make_manager
    from .llm import BENCH_SYSTEM, BENCH_USER, LocalStudioLLM, make_llm
    from .schemas import ChunkDigest

    started = time.time()
    result: dict = {"ok": False, "engine": engine_name(s), "model": None, "tokens_per_sec": None, "ttft": None,
                    "offloaded": None, "fits": None, "vram": None, "seconds": None, "message": None}
    builtin = s.effective_mode == "local" and getattr(s, "local_engine", "lmstudio") == "builtin"
    manager = manager or (make_manager(s, root=root, catalogue=catalogue) if builtin else None)
    was_running = bool(manager and manager.info())
    try:
        if manager is not None:
            result["model"] = manager.ensure_loaded()
        llm = LocalStudioLLM(s, auto_start=False) if s.effective_mode == "local" else make_llm(s)
        llm.complete_json(BENCH_SYSTEM, BENCH_USER, ChunkDigest, retries=0)
        stats = getattr(llm, "last_stats", None)
        result["model"] = result["model"] or getattr(llm, "model", None) or s.llm_model
        if stats is not None and getattr(stats, "output_tokens", 0):
            result["tokens_per_sec"] = round(stats.tokens_per_sec, 1)
            result["ttft"] = None if stats.ttft_seconds is None else round(stats.ttft_seconds, 2)
        if manager is not None:
            result["offloaded"] = manager.offload()
            used, total = manager.vram_used_total()
            result["vram"] = [used, total] if used is not None else None
            on_card = result["offloaded"]
            fits = fits_in_dedicated(used, total)
            if on_card and on_card[1]:
                result["fits"] = on_card[0] == on_card[1] and fits is not False
        result["ok"] = True
    except GpuInterrupted:
        result["message"] = "AI work is paused. Resume it, then test again."
    except Exception as e:  # noqa: BLE001
        result["message"] = str(e) or e.__class__.__name__
    finally:
        if manager is not None and not was_running:
            try:
                manager.unload_chat()
            except Exception:  # noqa: BLE001
                log.warning("Could not stop llama-server after the test", exc_info=True)
    result["seconds"] = round(time.time() - started, 1)
    return result


def engine_name(s) -> str:
    """builtin | lmstudio | ollama | cloud: the four choices on the AI engine screen."""
    if s.effective_mode != "local":
        return "cloud"
    return {"server": "ollama"}.get(getattr(s, "local_engine", "lmstudio"), getattr(s, "local_engine", "lmstudio"))
