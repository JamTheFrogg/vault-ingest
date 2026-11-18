"""The built-in AI's files: the catalogue (configs/models.json), the hardware check, and the downloader.

Everything lands in the app's data folder (`config.home()`): models/<entry id>/, bin/llama/<tag>-<backend>/,
pylibs/ (the NVIDIA wheels). Nothing is downloaded until `Downloads.start` is called, which only the user's
click in the app does.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import tarfile
import threading
import time
import zipfile
from collections import deque
from pathlib import Path
from typing import Callable

import httpx

from .config import PROJECT_DIR

log = logging.getLogger("vault_ingest")
CATALOGUE = PROJECT_DIR / "configs" / "models.json"
KINDS = ("llama-server", "chat", "whisper", "gpu-libs", "embedding")
OSES = ("windows", "linux", "macos")
COMPLETE = ".complete"
RETRY_SECONDS = (5, 20, 60)                       # waits after a network error; then the download is "failed"
PUBLISH_SECONDS = 0.5


# ------------------------------------------------------------------ catalogue
def load_catalogue(path: Path | None = None) -> dict:
    cat = json.loads(Path(path or CATALOGUE).read_text(encoding="utf-8"))
    problems = validate(cat)
    if problems:
        raise ValueError(f"{path or CATALOGUE}: " + "; ".join(problems))
    return cat


def validate(cat: dict) -> list[str]:
    """What is wrong with a catalogue; empty when it can be used."""
    problems = []
    entries = cat.get("entries") if isinstance(cat, dict) else None
    if not isinstance(entries, list) or not entries:
        return ["no entries"]
    ids = [e.get("id") for e in entries if isinstance(e, dict)]
    if len(ids) != len(set(ids)) or len(ids) != len(entries):
        problems.append("every entry needs a unique id")
    for e in entries:
        if not isinstance(e, dict):
            continue
        where = e.get("id", "?")
        if e.get("kind") not in KINDS:
            problems.append(f"{where}: kind must be one of {KINDS}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", str(e.get("id", ""))):
            problems.append(f"{where}: the id names a folder: lower case, digits, dots and dashes")
        for key in ("title", "licence"):
            if not isinstance(e.get(key), str) or not e[key]:
                problems.append(f"{where}: {key} is missing")
        if e.get("kind") in ("llama-server", "gpu-libs") and e.get("os") not in OSES:
            problems.append(f"{where}: os must be one of {OSES}")
        if e.get("kind") == "llama-server" and not e.get("backend"):
            problems.append(f"{where}: backend is missing")
        if e.get("kind") in ("chat", "embedding"):
            if not e.get("alias"):
                problems.append(f"{where}: alias is missing")
            if [f.get("role") for f in e.get("files", [])].count("model") != 1:
                problems.append(f"{where}: needs exactly one file with role model")
        if e.get("kind") == "whisper" and not e.get("name"):
            problems.append(f"{where}: name is missing")
        files = e.get("files")
        if not isinstance(files, list) or not files:
            problems.append(f"{where}: no files")
            continue
        names = [f.get("name") for f in files]
        if len(names) != len(set(names)):
            problems.append(f"{where}: two files with the same name")
        for f in files:
            label = f"{where}/{f.get('name')}"
            if not isinstance(f.get("name"), str) or Path(f["name"]).name != f["name"] or f["name"].startswith("."):
                problems.append(f"{label}: name must be a plain file name")
            if not str(f.get("url", "")).startswith("https://"):
                problems.append(f"{label}: url must be https")
            if not isinstance(f.get("size"), int) or f["size"] <= 0:
                problems.append(f"{label}: size must be a positive number of bytes")
            sha = f.get("sha256")
            if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
                problems.append(f"{label}: sha256 must be 64 lower-case hex digits or null")
            if not isinstance(f.get("size_exact"), bool):
                problems.append(f"{label}: size_exact must be true or false")
    for key, kind in (("default_chat", "chat"), ("light_chat", "chat"), ("embedding", "embedding")):
        if key == "embedding" and key not in cat:
            continue                                  # a catalogue without search by meaning is still valid
        if not any(e.get("id") == cat.get(key) and e.get("kind") == kind for e in entries if isinstance(e, dict)):
            problems.append(f"{key} must name a {kind} entry")
    return problems


def entry(cat: dict, entry_id: str) -> dict | None:
    return next((e for e in cat["entries"] if e["id"] == entry_id), None)


def chat_entry(cat: dict, model: str | None) -> dict:
    """The chat model for LOCAL_MODEL: an entry id or alias; `auto` or an unknown name is the default."""
    want = (model or "auto").strip().lower()
    for e in cat["entries"]:
        if e["kind"] == "chat" and want in (e["id"], e["alias"].lower()):
            return e
    return entry(cat, cat["default_chat"])


def this_os(system: str | None = None) -> str:
    system = (system or platform.system()).lower()
    return {"darwin": "macos", "windows": "windows"}.get(system, "linux")


# ------------------------------------------------------------------ where things are
def folder(e: dict, root: Path, cat: dict) -> Path:
    if e["kind"] == "llama-server":
        return root / "bin" / "llama" / f"{cat.get('llama_cpp', 'llama')}-{e['backend']}"
    if e["kind"] == "gpu-libs":
        return root / "pylibs"
    return root / "models" / e["id"]


def _marker(e: dict, root: Path, cat: dict) -> Path:
    # pylibs/ is shared with whatever else is installed there, so its marker carries the entry's name
    return folder(e, root, cat) / (f".{e['id']}{COMPLETE}" if e["kind"] == "gpu-libs" else COMPLETE)


def installed(e: dict, root: Path, cat: dict) -> bool:
    return _marker(e, root, cat).is_file()


def server_binary(cat: dict, root: Path, backend: str, os_name: str | None = None) -> Path | None:
    """llama-server of the downloaded build for this backend, or None."""
    os_name = os_name or this_os()
    e = next((x for x in cat["entries"] if x["kind"] == "llama-server" and x["os"] == os_name
              and x["backend"] == backend), None)
    if e is None or not installed(e, root, cat):
        return None
    try:
        rel = json.loads(_marker(e, root, cat).read_text(encoding="utf-8")).get("server")
    except (OSError, ValueError):
        return None
    path = folder(e, root, cat) / str(rel or "")
    return path if rel and path.is_file() else None


def model_files(e: dict, root: Path, cat: dict) -> dict[str, Path] | None:
    """{role: path} of a downloaded chat or embedding model, or None when it is not complete."""
    if not installed(e, root, cat):
        return None
    return {f.get("role", f["name"]): folder(e, root, cat) / f["name"] for f in e["files"]}


def whisper_source(name: str, root: Path | None = None, cat: dict | None = None) -> str:
    """The downloaded folder of Whisper `name`, else `name` itself (faster-whisper then uses its own cache)."""
    try:
        from .config import home

        root = root or home()
        cat = cat or load_catalogue()
    except (OSError, ValueError):
        return name
    for e in cat["entries"]:
        if e["kind"] == "whisper" and e["name"] == name and installed(e, root, cat):
            return str(folder(e, root, cat))
    return name


def add_gpu_libs(root: Path | None = None) -> None:
    """Put the downloaded NVIDIA wheels on sys.path; `transcribe.add_nvidia_dll_directories` finds them there.
    The bundled interpreter runs with -I, so PYTHONPATH cannot do this."""
    import sys

    from .config import home

    pylibs = str((root or home()) / "pylibs")
    if os.path.isdir(pylibs) and pylibs not in sys.path:
        sys.path.append(pylibs)


# ------------------------------------------------------------------ hardware
def _nvidia_smi(which=shutil.which, run=None) -> str | None:
    import subprocess

    exe = which("nvidia-smi")
    if not exe:
        return None
    try:
        proc = (run or subprocess.run)([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                       capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def parse_gpus(text: str | None) -> list[tuple[str, int]]:
    """(name, MB) per card from `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits`."""
    gpus = []
    for line in (text or "").splitlines():
        name, _, mb = line.rpartition(",")
        if name.strip() and mb.strip().isdigit():
            gpus.append((name.strip(), int(mb.strip())))
    return gpus


def _ram_mb() -> int | None:
    try:
        import psutil

        return int(psutil.virtual_memory().total // 2**20)
    except Exception:  # noqa: BLE001
        return None


def hardware(cat: dict, *, nvidia_text: str | None = None, system: str | None = None, machine: str | None = None,
             ram_mb: int | None = None, smi: Callable[[], str | None] = _nvidia_smi) -> dict:
    """What this computer has and what to use: NVIDIA (VRAM from nvidia-smi), Apple Silicon, else the processor."""
    os_name = this_os(system)
    machine = (machine or platform.machine()).lower()
    ram_mb = ram_mb if ram_mb is not None else _ram_mb()
    gpus = parse_gpus(nvidia_text if nvidia_text is not None else smi())
    big, light = entry(cat, cat["default_chat"]), entry(cat, cat["light_chat"])
    if gpus:
        name, vram = max(gpus, key=lambda g: g[1])
        fits = vram >= big.get("min_vram_mb", 0)
        hw = {"gpu": "nvidia", "gpu_name": name, "vram_mb": vram,
              "backend": "cuda" if os_name == "windows" else "vulkan", "whisper_device": "cuda",
              "recommend": big["id"] if fits else light["id"],
              "reason": (f"{name} with {vram / 1024:.0f} GB: {'good enough for' if fits else 'too small for'} "
                         f"{big['title']}.")}
    elif os_name == "macos" and machine in ("arm64", "aarch64"):
        fits = (ram_mb or 0) >= big.get("min_ram_mb", 0)
        hw = {"gpu": "apple", "gpu_name": "Apple Silicon", "vram_mb": None, "backend": "metal",
              "whisper_device": "cpu", "recommend": big["id"] if fits else light["id"],
              "reason": f"Apple Silicon with {(ram_mb or 0) / 1024:.0f} GB: "
                        f"{'good enough for' if fits else 'too little memory for'} {big['title']}."}
    else:
        # the honest default without a graphics card: the light model is slow there and writes weaker notes
        hw = {"gpu": "none", "gpu_name": None, "vram_mb": None, "backend": "cpu", "whisper_device": "cpu",
              "recommend": "cloud",
              "reason": f"No graphics card found. {light['title']} runs on the processor, slowly; "
                        "the Claude API writes better notes."}
    if os.getenv("LLAMA_BACKEND"):              # e.g. vulkan for an AMD or Intel card, which this check cannot see
        hw["backend"] = os.environ["LLAMA_BACKEND"].strip().lower()
    return {**hw, "os": os_name, "ram_mb": ram_mb}


def for_machine(cat: dict, hw: dict) -> list[dict]:
    """The entries this computer can use, in the order the screen lists them."""
    rows = [e for e in cat["entries"] if e["kind"] == "llama-server" and e["os"] == hw["os"]
            and e["backend"] == hw["backend"]]
    rows += [e for e in cat["entries"] if e["kind"] == "chat"]
    rows += [e for e in cat["entries"] if e["kind"] == "whisper"]
    if hw["gpu"] == "nvidia":
        rows += [e for e in cat["entries"] if e["kind"] == "gpu-libs" and e["os"] == hw["os"]]
    embed_server = embedding_server_entry(cat, hw["os"])
    if embed_server is not None and embed_server not in rows and cat.get("embedding"):
        rows.append(embed_server)                 # search by meaning runs on the processor build
    rows += [e for e in cat["entries"] if e["kind"] == "embedding"]
    return rows


def embedding_server_entry(cat: dict, os_name: str | None = None) -> dict | None:
    """The llama.cpp build the embedding server runs: the processor build (the Mac build also runs on the
    processor), so search by meaning never takes video memory from Whisper or the chat model."""
    os_name = os_name or this_os()
    backend = "metal" if os_name == "macos" else "cpu"
    return next((e for e in cat["entries"] if e["kind"] == "llama-server" and e["os"] == os_name
                 and e["backend"] == backend), None)


# ------------------------------------------------------------------ downloads
class Cancelled(Exception):
    pass


class Damaged(Exception):
    pass


class DiskFull(Exception):
    pass


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _total(response: httpx.Response, have: int) -> int | None:
    match = re.search(r"/(\d+)$", response.headers.get("content-range", ""))
    if match:
        return int(match.group(1))
    length = response.headers.get("content-length")
    return have + int(length) if length and length.isdigit() else None


def _extract(archive: Path, dest: Path) -> None:
    if archive.name.endswith((".zip", ".whl")):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                target = (dest / name).resolve()
                if dest.resolve() not in target.parents and target != dest.resolve():
                    raise Damaged(f"{archive.name} has a file outside its folder: {name}")
            z.extractall(dest)
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")


class Downloads:
    """One download at a time, in the order started. Progress goes to `publish` as `{id, state, done, total,
    speed, message}`; state is queued, downloading, waiting (for the network), verifying, installing, done,
    paused (cancelled; the partial file stays, so it resumes) or failed."""

    def __init__(self, root: Path, cat: dict, publish: Callable[[dict], None], *,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] | None = None,
                 retries: tuple[float, ...] = RETRY_SECONDS, free: Callable[[Path], int] | None = None):
        self.root, self.cat, self.publish = Path(root), cat, publish
        self.transport, self.retries = transport, retries
        self._stop = threading.Event()
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self._free = free or (lambda p: shutil.disk_usage(p).free)
        self._lock = threading.Lock()
        self._queue: deque[str] = deque()
        self._cancel: dict[str, threading.Event] = {}
        self.jobs: dict[str, dict] = {}
        self._worker: threading.Thread | None = None

    # ---- what the screen asks
    def status(self, e: dict) -> dict:
        if installed(e, self.root, self.cat):
            return {"id": e["id"], "state": "done", "done": self.size(e), "total": self.size(e), "speed": 0, "message": None}
        job = self.jobs.get(e["id"])
        if job and job["state"] != "done":
            return {k: v for k, v in job.items() if not k.startswith("_")}
        done = self._have(e)
        return {"id": e["id"], "state": "paused" if done else "missing", "done": done, "total": self.size(e),
                "speed": 0, "message": None}

    @staticmethod
    def size(e: dict) -> int:
        return sum(f["size"] for f in e["files"])

    def start(self, entry_id: str) -> dict:
        e = entry(self.cat, entry_id)
        if e is None:
            raise KeyError(entry_id)
        with self._lock:
            busy = entry_id in self._queue or (self.jobs.get(entry_id) or {}).get("state") in (
                "downloading", "waiting", "verifying", "installing")
            if not busy and not installed(e, self.root, self.cat):
                self._queue.append(entry_id)
                self._cancel[entry_id] = threading.Event()
                self._set(e, "queued")
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._run, name="downloads", daemon=True)
                    self._worker.start()
        return self.status(e)

    def cancel(self, entry_id: str) -> dict:
        e = entry(self.cat, entry_id)
        if e is None:
            raise KeyError(entry_id)
        with self._lock:
            if entry_id in self._queue:
                self._queue.remove(entry_id)
                self._set(e, "paused")
            elif entry_id in self._cancel:
                self._cancel[entry_id].set()
        return self.status(e)

    def stop(self) -> None:
        """At shutdown: every running download stops where it is and resumes next time."""
        self._stop.set()
        for event in list(self._cancel.values()):
            event.set()

    # ---- the worker
    def _set(self, e: dict, state: str, *, done: int | None = None, total: int | None = None, speed: float = 0,
             message: str | None = None, force: bool = True) -> None:
        job = self.jobs.setdefault(e["id"], {"id": e["id"], "done": 0, "total": self.size(e), "_sent": 0.0})
        changed = job.get("state") != state
        job.update(state=state, speed=round(speed), message=message)
        if done is not None:
            job["done"] = done
        if total is not None:
            job["total"] = total
        now = time.monotonic()
        if force or changed or now - job["_sent"] >= PUBLISH_SECONDS:
            job["_sent"] = now
            self.publish({k: v for k, v in job.items() if not k.startswith("_")})

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                entry_id = self._queue.popleft()
            e = entry(self.cat, entry_id)
            try:
                self._one(e, self._cancel[entry_id])
            except Cancelled:
                self._set(e, "paused", done=self._have(e))
            except DiskFull as ex:
                self._set(e, "failed", message=str(ex))
            except Exception as ex:  # noqa: BLE001  any other failure is shown on the row, with Try again
                log.warning("Download of %s failed: %s", entry_id, ex)
                self._set(e, "failed", message=str(ex) or ex.__class__.__name__)

    def _have(self, e: dict) -> int:
        """Bytes already on disk: finished files and partial ones."""
        have = 0
        for f in e["files"]:
            target = self._target(e, f)
            part = target.with_name(target.name + ".part")
            have += target.stat().st_size if target.is_file() else part.stat().st_size if part.is_file() else 0
        return have

    def _target(self, e: dict, f: dict) -> Path:
        if e["kind"] in ("llama-server", "gpu-libs"):          # archives: unpacked, then the download is removed
            return self.root / "downloads" / e["id"] / f["name"]
        return folder(e, self.root, self.cat) / f["name"]

    def _one(self, e: dict, cancel: threading.Event) -> None:
        total = self.size(e)
        targets = [(f, self._target(e, f)) for f in e["files"]]
        targets[0][1].parent.mkdir(parents=True, exist_ok=True)
        need, free = total - self._have(e), self._free(targets[0][1].parent)
        if need > free:
            raise DiskFull(f"Needs {(need - free) / 1e9:.1f} GB more free space.")
        base = 0
        for f, target in targets:
            if not target.is_file():
                self._file(e, f, target, cancel, base, total)
            base += target.stat().st_size
        self._set(e, "installing", done=base, total=base)
        self._install(e, [t for _f, t in targets])
        self._set(e, "done", done=base, total=base)
        log.info("Downloaded %s (%d bytes).", e["id"], base)

    def _file(self, e: dict, f: dict, target: Path, cancel: threading.Event, base: int, total: int) -> None:
        part = target.with_name(target.name + ".part")
        waits = list(self.retries)
        damaged = 0
        while True:
            try:
                announced = self._fetch(e, f, part, cancel, base, total)
                self._set(e, "verifying", done=base + part.stat().st_size)
                self._verify(f, part, announced)
                os.replace(part, target)
                return
            except Damaged:
                part.unlink(missing_ok=True)
                damaged += 1
                if damaged > 1:
                    raise Damaged(f"{f['name']} was damaged twice in a row. Try again later.") from None
                self._set(e, "downloading", done=base, message="The download was damaged. Downloading it again.")
            except httpx.HTTPError as ex:
                status = ex.response.status_code if isinstance(ex, httpx.HTTPStatusError) else None
                if cancel.is_set():
                    raise Cancelled() from None
                if (status is not None and status < 500 and status != 429) or not waits:
                    raise
                wait = waits.pop(0)
                log.info("Download of %s: %s; trying again in %.0f s.", f["name"], ex, wait)
                self._set(e, "waiting", done=self._have(e), message="Waiting for the network")
                self._sleep(wait)
                if cancel.is_set():
                    raise Cancelled() from None

    def _fetch(self, e: dict, f: dict, part: Path, cancel: threading.Event, base: int, total: int) -> int | None:
        """Append the rest of the file to `part`. Returns the full length the server announced, if it did."""
        have = part.stat().st_size if part.is_file() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with httpx.Client(transport=self.transport, follow_redirects=True,
                          timeout=httpx.Timeout(30.0, read=60.0)) as client:
            with client.stream("GET", f["url"], headers=headers) as r:
                if r.status_code == 416 and have:
                    return None                              # nothing left to fetch; the check decides
                if r.status_code == 200:
                    have = 0                                 # the server ignored the range: start over
                elif r.status_code != 206:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code} for {f['name']}", request=r.request, response=r)
                expected = _total(r, have)
                started, at_start = time.monotonic(), have
                with open(part, "ab" if have else "wb") as fh:
                    for block in r.iter_bytes():        # as it arrives: a cut connection keeps what came
                        if cancel.is_set():
                            raise Cancelled()
                        fh.write(block)
                        have += len(block)
                        elapsed = time.monotonic() - started
                        self._set(e, "downloading", done=base + have, total=total,
                                  speed=(have - at_start) / elapsed if elapsed > 0.5 else 0, force=False)
                if expected is not None and have < expected:
                    raise httpx.ReadError(f"{f['name']}: the connection ended at {have} of {expected} bytes")
                return expected

    @staticmethod
    def _verify(f: dict, part: Path, announced: int | None) -> None:
        size = part.stat().st_size
        if f.get("sha256"):
            if sha256_of(part) != f["sha256"]:
                raise Damaged(f"{f['name']}: SHA-256 does not match")
        elif f.get("size_exact"):
            if size != f["size"]:
                raise Damaged(f"{f['name']}: {size} bytes, expected {f['size']}")
        elif announced is not None and size != announced:                 # size check only
            raise Damaged(f"{f['name']}: {size} bytes, the server announced {announced}")

    def _install(self, e: dict, files: list[Path]) -> None:
        dest = folder(e, self.root, self.cat)
        info: dict = {"id": e["id"], "files": [p.name for p in files]}
        if e["kind"] in ("llama-server", "gpu-libs"):
            dest.mkdir(parents=True, exist_ok=True)
            for archive in files:
                # a wheel of these libraries is plain files under nvidia/: unpacking it is what pip --target does
                _extract(archive, dest)
            if e["kind"] == "llama-server":
                exe = "llama-server.exe" if e["os"] == "windows" else "llama-server"
                found = sorted(dest.rglob(exe), key=lambda p: len(p.parts))
                if not found:
                    raise Damaged(f"{exe} is not in the downloaded archive")
                if os.name != "nt":
                    found[0].chmod(found[0].stat().st_mode | 0o111)
                info["server"] = found[0].relative_to(dest).as_posix()
            for archive in files:
                archive.unlink(missing_ok=True)          # our own download, now unpacked
        _marker(e, self.root, self.cat).write_text(json.dumps(info) + "\n", encoding="utf-8")
