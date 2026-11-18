"""Search by meaning: notes cut into passages, embedded by a second llama-server on the processor.

The engine starts the server (`EmbeddingServer`, `<home>/state/llama-embed.json`) once the model is downloaded and
keeps the vectors in the note index current. The MCP server only reads that state file to embed a query, so with
the app closed it falls back to keyword search. `EMBEDDING_BASE_URL` (+ `EMBEDDING_MODEL`) points both at an
OpenAI-compatible address instead (LM Studio, Ollama), as the old setup did; `EMBEDDINGS=off` turns it all off.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx

from . import models
from .llama_server import LlamaServerManager

log = logging.getLogger("vault_ingest")
STATE_FILE = "llama-embed.json"
MAX_CHARS = 1200                 # about 300-500 tokens: well inside the server's batch, one idea per passage
BATCH = 16                       # passages per request
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")


class EmbeddingUnavailable(RuntimeError):
    pass


# ------------------------------------------------------------------ passages
def chunk_note(title: str, body: str, max_chars: int = MAX_CHARS) -> list[tuple[str | None, str]]:
    """(heading, text) passages of a note body: cut at headings, then packed by paragraph up to `max_chars`.
    A note without text is one passage of its title, so it can still be found by name."""
    pieces: list[tuple[str | None, str]] = []
    heading: str | None = None
    paragraphs: list[str] = []

    def flush() -> None:
        current = ""
        for para in paragraphs:
            for part in _split(para, max_chars):
                if current and len(current) + len(part) + 2 > max_chars:
                    pieces.append((heading, current))
                    current = ""
                current = f"{current}\n\n{part}" if current else part
        if current:
            pieces.append((heading, current))
        paragraphs.clear()

    block: list[str] = []
    lines = re.sub(r"\A\s*# [^\n]*\n", "", body or "").splitlines()   # the note's own `# Title` is the title
    for line in lines + [""]:
        m = HEADING.match(line)
        if m or not line.strip():
            if block:
                paragraphs.append(" ".join(x.strip() for x in block))
                block = []
            if m:
                flush()
                heading = m.group(2).strip()
            continue
        block.append(line)
    flush()
    return pieces or [(None, title)]


def _split(text: str, max_chars: int) -> list[str]:
    """A paragraph longer than `max_chars` cut at sentence ends, else at spaces."""
    if len(text) <= max_chars:
        return [text]
    out, current = [], ""
    for part in re.split(r"(?<=[.!?…])\s+|\s+", text):
        if current and len(current) + len(part) + 1 > max_chars:
            out.append(current)
            current = ""
        current = f"{current} {part}" if current else part[:max_chars]
    return out + ([current] if current else [])


def embed_input(title: str, heading: str | None, text: str) -> str:
    """What the model reads for one passage: the note's title and heading give it its context."""
    return "\n".join(x for x in (title, heading, "", text) if x is not None)


# ------------------------------------------------------------------ the HTTP client
class EmbeddingClient:
    """POST /v1/embeddings on llama-server or any OpenAI-compatible address."""

    def __init__(self, base_url: str, model: str, key: str = "", *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.model, self.key, self.transport, self.timeout = model, key, transport, timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
            for start in range(0, len(texts), BATCH):
                batch = texts[start:start + BATCH]
                try:
                    r = client.post(f"{self.base_url}/embeddings", json={"model": self.model, "input": batch},
                                    headers={"Authorization": f"Bearer {self.key}"} if self.key else {})
                    r.raise_for_status()
                    data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                    vectors = [d["embedding"] for d in data]
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                    raise EmbeddingUnavailable(f"The embedding server did not answer: {e}") from e
                if len(vectors) != len(batch) or not all(isinstance(v, list) and v for v in vectors):
                    raise EmbeddingUnavailable("The embedding server sent a wrong number of vectors.")
                out += vectors
        return out


# ------------------------------------------------------------------ the server
class EmbeddingServer(LlamaServerManager):
    """llama-server --embedding with the embedding model, on the processor: never in video memory, so it cannot
    compete with Whisper or the chat model. Shared through its state file, like the chat server."""
    STATE, LOG = STATE_FILE, "llama-embed.log"

    def __init__(self, settings=None, *, root: Path | None = None, catalogue: dict | None = None, **kw):
        super().__init__(settings, root=root, catalogue=catalogue, **kw)
        self.entry = models.entry(self.cat, self.cat.get("embedding", ""))

    def _argv(self, port: int) -> tuple[Path, list[str], str]:
        build = models.embedding_server_entry(self.cat)
        server = models.server_binary(self.cat, self.root, build["backend"]) if build else None
        files = models.model_files(self.entry, self.root, self.cat) if self.entry else None
        if server is None or files is None:
            raise EmbeddingUnavailable("Search by meaning is not downloaded. Open Settings > AI engine.")
        threads = max(1, (os.cpu_count() or 2) // 2)            # leaves half the processor to everything else
        # --device none -ngl 0: nothing on a graphics card, even with a GPU build; -ub = -c: a passage is one batch
        argv = [str(server), "-m", str(files["model"]), "--embedding", "-c", "2048", "-b", "2048", "-ub", "2048",
                "-ngl", "0", "--device", "none", "-t", str(threads), "--host", "127.0.0.1", "--port", str(port),
                "--alias", self.entry["alias"]]
        return server, argv, self.entry["alias"]

    def _use(self, info: dict) -> None:
        return None                                             # the chat settings stay as they are

    def client(self, info: dict | None = None) -> EmbeddingClient | None:
        info = info or self.info()
        if not info or not self.healthy(info):
            return None
        return EmbeddingClient(f"http://127.0.0.1:{info['port']}/v1", self.entry["alias"], info.get("key", ""))


def enabled() -> bool:
    return (os.getenv("EMBEDDINGS") or "on").strip().lower() not in ("off", "0", "false", "no")


def model_id(root: Path, cat: dict) -> str | None:
    """The name the vectors are stored under; None when search by meaning is off or not downloaded."""
    if not enabled():
        return None
    if os.getenv("EMBEDDING_BASE_URL"):
        return "address:" + (os.getenv("EMBEDDING_MODEL") or "embedding")
    e = models.entry(cat, cat.get("embedding", ""))
    return e["id"] if e and models.installed(e, root, cat) else None


def running_client(root: Path, cat: dict) -> EmbeddingClient | None:
    """A client for whatever already runs; never starts anything (the MCP server's side)."""
    if not enabled():
        return None
    address = os.getenv("EMBEDDING_BASE_URL")
    if address:
        return EmbeddingClient(address, os.getenv("EMBEDDING_MODEL") or "embedding", os.getenv("EMBEDDING_API_KEY", ""))
    if model_id(root, cat) is None:
        return None
    return EmbeddingServer(root=root, catalogue=cat).client()
