import hashlib
import io
import json
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vault_ingest import models
from vault_ingest.models import Downloads, hardware, load_catalogue, validate

CAT = load_catalogue()


# ------------------------------------------------------------------ catalogue
def test_the_shipped_catalogue_is_valid_and_has_every_entry_the_plan_names():
    assert validate(CAT) == []
    ids = {e["id"] for e in CAT["entries"]}
    assert {"llama-win-cuda", "llama-win-vulkan", "llama-win-cpu", "llama-linux-vulkan", "llama-linux-cpu",
            "llama-macos-metal", "gemma-4-12b", "gemma-4-e4b", "whisper-large-v3", "cuda-libs-win"} <= ids
    assert CAT["default_chat"] == "gemma-4-12b" and CAT["light_chat"] == "gemma-4-e4b"
    cuda = models.entry(CAT, "llama-win-cuda")
    assert [f["name"] for f in cuda["files"]] == ["llama-b12380-bin-win-cuda-12.4-x64.zip",
                                                    "cudart-llama-bin-win-cuda-12.4-x64.zip"]
    for e in CAT["entries"]:
        for f in e["files"]:
            # either a hash, or the size check: never neither
            assert f["sha256"] or f["size_exact"] is not None


def test_the_two_projectors_with_the_same_name_live_in_their_own_folders(tmp_path):
    big, light = models.entry(CAT, "gemma-4-12b"), models.entry(CAT, "gemma-4-e4b")
    assert models.folder(big, tmp_path, CAT) != models.folder(light, tmp_path, CAT)
    assert {f["name"] for f in big["files"]} & {f["name"] for f in light["files"]} == {"mmproj-F16.gguf"}


@pytest.mark.parametrize("change, problem", [
    (lambda c: c["entries"][0]["files"][0].update(url="http://x/a"), "url must be https"),
    (lambda c: c["entries"][0]["files"][0].update(sha256="abc"), "sha256 must be"),
    (lambda c: c["entries"][0]["files"][0].update(name="../evil.exe"), "plain file name"),
    (lambda c: c["entries"][0]["files"][0].update(size=0), "positive number"),
    (lambda c: c["entries"][0]["files"][0].pop("size_exact"), "size_exact"),
    (lambda c: c["entries"].append(dict(c["entries"][0])), "unique id"),
    (lambda c: c.update(default_chat="llama-win-cpu"), "default_chat"),
    (lambda c: c["entries"][0].update(kind="other"), "kind must be"),
    (lambda c: c["entries"][0].pop("licence"), "licence is missing"),
])
def test_a_broken_catalogue_says_what_is_wrong(change, problem):
    cat = json.loads(json.dumps(CAT))
    change(cat)
    assert any(problem in p for p in validate(cat))


def test_changing_the_model_is_a_catalogue_edit(tmp_path):
    cat = json.loads(json.dumps(CAT))
    cat["default_chat"] = "gemma-4-e4b"
    path = tmp_path / "models.json"
    path.write_text(json.dumps(cat), encoding="utf-8")
    loaded = load_catalogue(path)
    assert models.chat_entry(loaded, "auto")["id"] == "gemma-4-e4b"
    assert models.chat_entry(loaded, "gemma-4-12b")["alias"] == "gemma-4-12b"


# ------------------------------------------------------------------ hardware
def test_an_8_gb_nvidia_card_gets_the_12b_and_cuda():
    hw = hardware(CAT, nvidia_text="NVIDIA GeForce RTX 3070, 8192\n", system="Windows", ram_mb=32768)
    assert hw["gpu"] == "nvidia" and hw["vram_mb"] == 8192
    assert hw["recommend"] == "gemma-4-12b" and hw["backend"] == "cuda" and hw["whisper_device"] == "cuda"
    rows = [e["id"] for e in models.for_machine(CAT, hw)]
    assert rows == ["llama-win-cuda", "gemma-4-12b", "gemma-4-e4b", "whisper-large-v3", "cuda-libs-win",
                    "llama-win-cpu", "bge-m3"]


def test_a_small_card_gets_the_light_model_and_the_biggest_card_counts():
    hw = hardware(CAT, nvidia_text="NVIDIA T400, 4096\nNVIDIA GeForce GTX 1650, 6144\n", system="Linux", ram_mb=16000)
    assert hw["gpu_name"] == "NVIDIA GeForce GTX 1650" and hw["recommend"] == "gemma-4-e4b"
    assert hw["backend"] == "vulkan"
    assert "cuda-libs-win" not in [e["id"] for e in models.for_machine(CAT, hw)]


def test_apple_silicon_by_memory():
    assert hardware(CAT, nvidia_text="", system="Darwin", machine="arm64", ram_mb=16384)["recommend"] == "gemma-4-12b"
    small = hardware(CAT, nvidia_text="", system="Darwin", machine="arm64", ram_mb=8192)
    assert small["recommend"] == "gemma-4-e4b" and small["backend"] == "metal" and small["whisper_device"] == "cpu"


def test_no_graphics_card_recommends_a_cloud_key():
    hw = hardware(CAT, nvidia_text=None, system="Windows", ram_mb=16000, smi=lambda: None)
    assert hw["gpu"] == "none" and hw["recommend"] == "cloud" and hw["backend"] == "cpu"
    assert [e["id"] for e in models.for_machine(CAT, hw)][0] == "llama-win-cpu"


def test_nvidia_smi_output_that_is_not_a_table_means_no_card():
    assert models.parse_gpus("NVIDIA-SMI has failed because it couldn't communicate with the driver.") == []


# ------------------------------------------------------------------ downloads against a local server
class Files:
    """What the test server serves, and how it misbehaves."""

    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.ignore_range = False
        self.cut_after: int | None = None          # drop the connection after this many bytes, once
        self.slow = 0.0
        self.ranges: list[str | None] = []


@pytest.fixture
def server():
    files = Files()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = files.data.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            rng = self.headers.get("Range")
            files.ranges.append(rng)
            start = int(rng.split("=")[1].rstrip("-")) if rng and not files.ignore_range else 0
            if start >= len(body) and rng:
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206 if start else 200)
            if start:
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            sent = 0
            for i in range(start, len(body), 4096):
                if files.cut_after is not None and sent >= files.cut_after:
                    files.cut_after = None
                    return                                 # the connection ends early
                self.wfile.write(body[i:i + 4096])
                sent += 4096
                if files.slow:
                    time.sleep(files.slow)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    files.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield files
    httpd.shutdown()


def chat_catalogue(base: str, payload: bytes, *, sha: str | None = "right", size_exact=False) -> dict:
    digest = hashlib.sha256(payload).hexdigest() if sha == "right" else sha
    return {"llama_cpp": "b1", "default_chat": "tiny", "light_chat": "tiny", "entries": [
        {"id": "tiny", "kind": "chat", "alias": "tiny", "title": "Tiny", "licence": "MIT", "files": [
            {"name": "tiny.gguf", "role": "model", "url": f"{base}/tiny.gguf", "size": len(payload),
             "size_exact": size_exact, "sha256": digest}]}]}


def run(d: Downloads, entry_id: str, timeout: float = 20) -> list[dict]:
    d.start(entry_id)
    deadline = time.time() + timeout
    while d._worker is not None and d._worker.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    return d.jobs[entry_id]


def test_a_download_is_checked_and_lands_in_the_models_folder(server, tmp_path):
    payload = bytes(range(256)) * 4000
    server.data["/tiny.gguf"] = payload
    events = []
    cat = chat_catalogue(server.base, payload)
    d = Downloads(tmp_path, cat, events.append)
    assert d.status(cat["entries"][0])["state"] == "missing"     # nothing happens before start
    job = run(d, "tiny")
    assert job["state"] == "done", job
    target = tmp_path / "models" / "tiny" / "tiny.gguf"
    assert target.read_bytes() == payload and not target.with_name("tiny.gguf.part").exists()
    assert models.model_files(cat["entries"][0], tmp_path, cat) == {"model": target}
    states = [e["state"] for e in events]
    assert states[0] == "queued" and "downloading" in states and "verifying" in states and states[-1] == "done"
    assert d.start("tiny")["state"] == "done"                     # nothing downloaded twice


def test_a_download_resumes_with_a_range_request(server, tmp_path):
    payload = b"x" * 50_000 + b"y" * 50_000
    server.data["/tiny.gguf"] = payload
    cat = chat_catalogue(server.base, payload)
    part = tmp_path / "models" / "tiny" / "tiny.gguf.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(payload[:30_000])
    d = Downloads(tmp_path, cat, lambda e: None)
    before = d.status(cat["entries"][0])
    assert before["state"] == "paused" and before["done"] == 30_000
    assert run(d, "tiny")["state"] == "done"
    assert server.ranges == ["bytes=30000-"]
    assert (tmp_path / "models" / "tiny" / "tiny.gguf").read_bytes() == payload


def test_a_server_that_ignores_the_range_is_downloaded_again_from_the_start(server, tmp_path):
    payload = b"abc" * 20_000
    server.data["/tiny.gguf"] = payload
    server.ignore_range = True
    cat = chat_catalogue(server.base, payload)
    part = tmp_path / "models" / "tiny" / "tiny.gguf.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"zzz" * 1000)
    assert run(Downloads(tmp_path, cat, lambda e: None), "tiny")["state"] == "done"
    assert (tmp_path / "models" / "tiny" / "tiny.gguf").read_bytes() == payload


def test_a_bad_hash_is_downloaded_once_more_then_fails_and_keeps_nothing(server, tmp_path):
    payload = b"model" * 10_000
    server.data["/tiny.gguf"] = payload
    events = []
    cat = chat_catalogue(server.base, payload, sha="0" * 64)
    job = run(Downloads(tmp_path, cat, events.append), "tiny")
    assert job["state"] == "failed" and "damaged twice" in job["message"]
    assert any(e["message"] == "The download was damaged. Downloading it again." for e in events)
    assert not list((tmp_path / "models" / "tiny").iterdir())
    assert not models.installed(cat["entries"][0], tmp_path, cat)


def test_without_a_hash_the_size_the_server_announced_is_checked(server, tmp_path):
    payload = b"q" * 20_000
    server.data["/tiny.gguf"] = payload
    cat = chat_catalogue(server.base, payload, sha=None)
    cat["entries"][0]["files"][0]["size"] = 19_000               # the published, rounded size
    assert run(Downloads(tmp_path, cat, lambda e: None), "tiny")["state"] == "done"
    exact = chat_catalogue(server.base, payload, sha=None, size_exact=True)
    exact["entries"][0]["files"][0]["size"] = 19_000
    assert run(Downloads(tmp_path / "b", exact, lambda e: None), "tiny")["state"] == "failed"


def test_cancel_keeps_the_partial_file_and_start_resumes_it(server, tmp_path):
    payload = b"s" * 400_000
    server.data["/tiny.gguf"] = payload
    server.slow = 0.005
    cat = chat_catalogue(server.base, payload)
    d = Downloads(tmp_path, cat, lambda e: None)
    d.start("tiny")
    part = tmp_path / "models" / "tiny" / "tiny.gguf.part"
    deadline = time.time() + 10
    while not (part.is_file() and part.stat().st_size > 40_000) and time.time() < deadline:
        time.sleep(0.01)
    d.cancel("tiny")
    while d._worker is not None and d._worker.is_alive():
        time.sleep(0.01)
    assert d.jobs["tiny"]["state"] == "paused"
    kept = part.stat().st_size
    assert 0 < kept < len(payload)
    server.slow = 0
    assert run(d, "tiny")["state"] == "done"
    assert server.ranges[-1] == f"bytes={kept}-"
    assert (tmp_path / "models" / "tiny" / "tiny.gguf").read_bytes() == payload


def test_a_dropped_connection_waits_for_the_network_and_continues(server, tmp_path):
    payload = b"n" * 100_000
    server.data["/tiny.gguf"] = payload
    server.cut_after = 40_000
    events, waits = [], []
    cat = chat_catalogue(server.base, payload)
    d = Downloads(tmp_path, cat, events.append, sleep=waits.append, retries=(5, 20))
    assert run(d, "tiny")["state"] == "done"
    assert waits == [5] and any(e["state"] == "waiting" and e["message"] == "Waiting for the network" for e in events)
    assert server.ranges[-1] and server.ranges[-1].startswith("bytes=")


def test_a_missing_file_fails_without_waiting(server, tmp_path):
    cat = chat_catalogue(server.base, b"x")
    waits = []
    job = run(Downloads(tmp_path, cat, lambda e: None, sleep=waits.append), "tiny")
    assert job["state"] == "failed" and "404" in job["message"] and waits == []


def test_not_enough_disk_space_says_how_much_is_missing(server, tmp_path):
    payload = b"d" * 10_000
    server.data["/tiny.gguf"] = payload
    cat = chat_catalogue(server.base, payload)
    cat["entries"][0]["files"][0]["size"] = 3_200_000_000
    job = run(Downloads(tmp_path, cat, lambda e: None, free=lambda p: 0), "tiny")
    assert job["state"] == "failed" and job["message"] == "Needs 3.2 GB more free space."
    assert server.ranges == []


def tar_with(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_a_server_build_is_unpacked_and_found(server, tmp_path):
    archive = tar_with({"llama-b1/llama-server": b"#!/bin/sh\n", "llama-b1/libggml.so": b"lib"})
    server.data["/llama.tar.gz"] = archive
    cat = {"llama_cpp": "b1", "default_chat": "x", "light_chat": "x", "entries": [
        {"id": "llama-linux-cpu", "kind": "llama-server", "os": "linux", "backend": "cpu", "title": "t", "licence": "MIT",
         "files": [{"name": "llama.tar.gz", "url": f"{server.base}/llama.tar.gz", "size": len(archive),
                    "size_exact": True, "sha256": hashlib.sha256(archive).hexdigest()}]}]}
    assert run(Downloads(tmp_path, cat, lambda e: None), "llama-linux-cpu")["state"] == "done"
    exe = models.server_binary(cat, tmp_path, "cpu", "linux")
    assert exe == tmp_path / "bin" / "llama" / "b1-cpu" / "llama-b1" / "llama-server"
    assert exe.stat().st_mode & 0o111
    assert not (tmp_path / "downloads" / "llama-linux-cpu" / "llama.tar.gz").exists()   # the archive is gone


def test_gpu_wheels_are_unpacked_into_pylibs(server, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("nvidia/cublas/bin/cublas64_12.dll", b"dll")
    wheel = buf.getvalue()
    server.data["/w.whl"] = wheel
    cat = {"llama_cpp": "b1", "default_chat": "x", "light_chat": "x", "entries": [
        {"id": "cuda-libs-win", "kind": "gpu-libs", "os": "windows", "title": "t", "licence": "x",
         "files": [{"name": "w.whl", "url": f"{server.base}/w.whl", "size": len(wheel), "size_exact": True,
                    "sha256": hashlib.sha256(wheel).hexdigest()}]}]}
    assert run(Downloads(tmp_path, cat, lambda e: None), "cuda-libs-win")["state"] == "done"
    assert (tmp_path / "pylibs" / "nvidia" / "cublas" / "bin" / "cublas64_12.dll").read_bytes() == b"dll"
    assert models.installed(cat["entries"][0], tmp_path, cat)


def test_an_archive_that_writes_outside_its_folder_is_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../evil.txt", b"x")
    archive = tmp_path / "a.zip"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(models.Damaged):
        models._extract(archive, tmp_path / "out")
    assert not (tmp_path.parent / "evil.txt").exists()


def test_whisper_is_loaded_from_the_downloaded_folder(tmp_path):
    e = models.entry(CAT, "whisper-large-v3")
    assert models.whisper_source("large-v3", tmp_path, CAT) == "large-v3"      # not there: faster-whisper's cache
    target = models.folder(e, tmp_path, CAT)
    target.mkdir(parents=True)
    (target / ".complete").write_text("{}", encoding="utf-8")
    assert models.whisper_source("large-v3", tmp_path, CAT) == str(target)
    assert models.whisper_source("tiny", tmp_path, CAT) == "tiny"
