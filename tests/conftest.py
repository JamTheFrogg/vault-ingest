import json
import re
import shutil
from pathlib import Path

import pytest

from vault_ingest.config import Settings, PROJECT_DIR
from vault_ingest.llm import FakeLLM
from vault_ingest.pipeline import Pipeline

TEMPLATE = """---
type: client
client: Acme
status: active
last_contact:
tags: [client]
---
# Acme

## Snapshot
Who they are, what they do, who you deal with, how they like to work.

## Current Status
Where things stand right now (rewritten after each call).

## Open Items
- [ ] item — owner — due

## Decisions Log
- YYYY-MM-DD — decision (link to call)

## Preferences & Watch-outs

## Work Log
- YYYY-MM-DD — what was done (link)
"""

ACME_PROFILE = TEMPLATE.replace("Who they are, what they do, who you deal with, how they like to work.",
                                "Acme d.o.o. — e-commerce shop. Contact: Alex (CEO).") \
    .replace("- [ ] item — owner — due", "- [ ] Send revised proposal — Jane — Friday")

SENTENCES = [
    "We looked at the Q4 launch plan and the numbers are tight.",
    "The budget for paid ads is currently 4,000 euros per month.",
    "I think we should move part of that into email campaigns instead.",
    "Our conversion rate on the landing page dropped to 1.8 percent last month.",
    "Let's schedule the new product photos for the second week of October.",
    "The developer said the checkout bug is fixed on staging.",
    "We need legal to approve the new privacy text before launch.",
    "Customers keep asking about delivery times to Croatia.",
]


def make_transcript(words_target: int = 2500, speakers=("Jane", "Alex")) -> str:
    lines, total, i, sec = [], 0, 0, 0
    while total < words_target:
        speaker = speakers[i % len(speakers)]
        text = " ".join(SENTENCES[(i + k) % len(SENTENCES)] for k in range(3))
        h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
        lines.append(f"[{h:02d}:{m:02d}:{s:02d}] {speaker}: {text}")
        total += len(text.split())
        sec += 37
        i += 1
    return "\n".join(lines)


def fake_responder(client_guess=("Acme", 0.93), fail_profile=False):
    def respond(system: str, user: str) -> str:
        if "Which client is this call with" in user:
            c, conf = client_guess
            return json.dumps({"client": c, "confidence": conf, "reason": "names match"})
        m = re.search(r"Chunk (\d+) of (\d+)", user)
        if m and "<chunk>" in user:
            return json.dumps({"context": f"In the call with Acme, part {m.group(1)} covers the Q4 launch budget.",
                               "points": ["Paid ads budget is 4,000 EUR/month", "Conversion dropped to 1.8%"]})
        if "verbatim quote" in system:
            return json.dumps({
                "decisions": [{"claim": "Shift 1,000 EUR/month from paid ads to email",
                               "quote": "The budget for paid ads is currently 4,000 euros per month."}],
                "action_items": [
                    {"claim": "Book product photographer",
                     "quote": "Let's schedule the new product photos for the second week of October."},
                    {"claim": "Draft email campaign",
                     "quote": "I think we should move part of that into email campaigns instead."},
                ],
                "numbers": [
                    {"claim": "4,000 EUR/month ads",
                     "quote": "The budget for paid ads is currently 4,000 euros per month."},
                    {"claim": "1.8% conversion",
                     "quote": "Our conversion rate on the landing page dropped to 1.8 percent last month."},
                ],
            })
        if "Write the call note" in user:
            return json.dumps({
                "title": "Q4 launch planning",
                "participants": ["Jane", "Alex (Acme CEO)"],
                "summary": "Jane and Alex reviewed the Q4 launch. They agreed to shift budget to email. Photos are scheduled.",
                "key_points": ["Paid ads at 4,000 EUR/month", "Conversion 1.8%"],
                "decisions": ["Shift 1,000 EUR/month from paid ads to email"],
                "action_items": [{"task": "Book product photographer", "owner": "Alex", "due": "Oct 10"},
                                 {"task": "Draft email campaign", "owner": "Jane", "due": None}],
                "open_questions": ["Delivery times to Croatia?"],
                "notable_details": ["4,000 EUR/month ads", "1.8% conversion"],
            })
        if "Currently open items" in user:
            if fail_profile:
                return "not json at all"
            return json.dumps({
                "current_status": "- Q4 launch in planning (last call 2026-09-30).\n- Budget shifting to email.",
                "resolved_open_items": ["Send revised proposal — Jane — Friday"],
                "new_open_items": [{"task": "Book product photographer", "owner": "Alex", "due": "Oct 10"},
                                   {"task": "Draft email campaign", "owner": "Jane"}],
                "snapshot_additions": [],
                "preferences_additions": ["Alex prefers decisions summarized in writing after calls"],
            })
        raise AssertionError(f"unexpected prompt: {user[:200]}")
    return respond


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "Vault"
    (v / "00_Inbox").mkdir(parents=True)
    (v / "_System" / "Templates").mkdir(parents=True)
    (v / "_System" / "Templates" / "Client.md").write_text(TEMPLATE, encoding="utf-8")
    acme = v / "Clients" / "Acme"
    for sub in ("Calls", "Transcripts", "Work"):
        (acme / sub).mkdir(parents=True)
    (acme / "Acme.md").write_text(ACME_PROFILE, encoding="utf-8")
    (v / "Personal").mkdir()
    (v / "Personal" / "diary.md").write_text("private", encoding="utf-8")
    return v


@pytest.fixture
def settings(vault: Path) -> Settings:
    return Settings(vault_path=vault, prompts_dir=PROJECT_DIR / "prompts", llm_provider="fake")


def make_pipeline(settings, responder=None, dry_run=False):
    llm = None if dry_run else FakeLLM(responder or fake_responder())
    return Pipeline(settings, llm, dry_run=dry_run, concurrency=2)


# ---------------------------------------------------------------- a fake embedding server (llama-server's API)
CONCEPTS = {"money": ("invoice", "invoices", "payment", "paid", "pay", "budget", "euros", "price", "racun"),
            "deploy": ("staging", "server", "deploy", "release", "hosting"),
            "rest": ("holiday", "vacation", "leave", "break"),
            "design": ("logo", "brand", "colours", "font", "design")}


def fake_vector(text: str) -> list[float]:
    """One dimension per concept and one for every other word: similar meaning, similar vector."""
    words = re.findall(r"\w+", text.lower())
    vec = [float(sum(w in group for w in words)) for group in CONCEPTS.values()]
    return vec + [0.1 * len([w for w in words if not any(w in g for g in CONCEPTS.values())]) ** 0.5 or 0.01]


@pytest.fixture
def fake_embedder():
    """An OpenAI-compatible /v1/embeddings server on a free port; `.requests` counts the texts it embedded."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            texts = data["input"] if isinstance(data["input"], list) else [data["input"]]
            server.requests.extend(texts)
            body = json.dumps({"data": [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(texts)],
                               "model": data.get("model")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
