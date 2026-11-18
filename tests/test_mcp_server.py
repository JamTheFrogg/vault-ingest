import asyncio
import json
import os

import pytest
from mcp import Client

from vault_ingest import embeddings, mcp_server
from vault_ingest.embeddings import EmbeddingClient
from vault_ingest.mcp_server import VaultTools, build

NAMES = {"vault_list", "note_read", "note_read_many", "search_text", "search_semantic", "search_metadata",
         "wikilinks", "note_create", "note_write", "note_patch", "frontmatter"}


@pytest.fixture
def tools(vault, settings, tmp_path, monkeypatch):
    for key in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDINGS"):
        monkeypatch.delenv(key, raising=False)
    (vault / "Clients/Acme/Calls/2026-10-01 Billing.md").write_text(
        "---\ntype: call\nclient: Acme\ndate: 2026-10-01\ntags: [money]\n---\n# Billing\n\n"
        "Back to [[Clients/Acme/Acme|Acme]] and [[Nowhere]].\n\n## Summary\nThe invoice for September is open. #billing/late\n",
        encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian/app.md").write_text("x", encoding="utf-8")
    (vault / "00_Inbox/waiting.md").write_text("not yet", encoding="utf-8")
    t = VaultTools(settings, data_home=tmp_path / "home")
    yield t
    t.index.close()


def run(tools, *calls):
    """Each (name, arguments) through the SDK's in-memory client; (is_error, text) per call."""
    async def go():
        async with Client(build(tools)) as client:
            out = []
            for name, args in calls:
                r = await client.call_tool(name, args)
                out.append((r.is_error, r.content[0].text if r.content else ""))
            return out

    return asyncio.run(go())


def one(tools, name, args):
    error, text = run(tools, (name, args))[0]
    assert not error, text
    return text


def test_the_tools_and_their_arguments(tools):
    async def listed():
        async with Client(build(tools)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    found = asyncio.run(listed())
    assert set(found) == NAMES
    assert set(found["note_patch"].input_schema["required"]) == {"path", "operation", "target_type", "target", "content"}
    assert {"query", "top_k", "include_content", "lexical_prefetch", "alpha"} <= set(found["search_semantic"].input_schema["properties"])
    assert found["note_read"].annotations.read_only_hint and found["note_write"].annotations.destructive_hint


def test_read_list_and_search(tools, vault):
    assert one(tools, "note_read", {"path": "Clients/Acme/Acme"}).startswith("---\ntype: client")
    assert "[cut at 10 of" in one(tools, "note_read", {"path": "Clients/Acme/Acme.md", "max_bytes": 10})
    listed = json.loads(one(tools, "vault_list", {}))
    assert "Clients/" in listed and "_System/" in listed and "00_Inbox/" not in listed and ".obsidian/" not in listed
    every = json.loads(one(tools, "vault_list", {"recursive": True, "glob": "*.md"}))
    assert "Clients/Acme/Calls/2026-10-01 Billing.md" in every and all(p.endswith(".md") for p in every)
    assert "_System/Templates/Client.md" in every                    # rules in _System stay readable
    meta = json.loads(one(tools, "vault_list", {"path": "Clients/Acme/Calls", "include_metadata": True}))
    assert meta == [{"path": "Clients/Acme/Calls/2026-10-01 Billing.md", "title": "2026-10-01 Billing", "type": "call",
                     "client": "Acme", "date": "2026-10-01", "size": meta[0]["size"]}]
    assert one(tools, "vault_list", {"path": "Clients", "format": "tree"}).startswith("Acme/\n  Calls/\n    2026-10-01")
    [hit] = json.loads(one(tools, "search_text", {"query": "september invoice"}))
    assert hit["path"].endswith("Billing.md") and "**invoice**" in hit["snippet"]
    # no note has every word: notes with any of them
    assert json.loads(one(tools, "search_text", {"query": "invoice unicorn"}))[0]["path"].endswith("Billing.md")
    many = json.loads(one(tools, "note_read_many", {"paths": ["Clients/Acme/Acme.md", "Nope.md", "../x.md"]}))
    assert [n["path"] for n in many["notes"]] == ["Clients/Acme/Acme.md"]
    assert [s["reason"] for s in many["skipped"]] == ["not_found", "refused"]
    folder = json.loads(one(tools, "note_read_many", {"dir": "Clients/Acme", "recursive": True}))
    assert {n["path"] for n in folder["notes"]} == {"Clients/Acme/Acme.md", "Clients/Acme/Calls/2026-10-01 Billing.md"}


def test_links_and_metadata(tools):
    back = json.loads(one(tools, "wikilinks", {"query": "backlinks", "path": "Clients/Acme/Acme.md"}))
    assert [b["source_path"] for b in back] == ["Clients/Acme/Calls/2026-10-01 Billing.md"]
    out = json.loads(one(tools, "wikilinks", {"query": "outgoing", "path": "Clients/Acme/Calls/2026-10-01 Billing.md"}))
    assert [(x["target"], x["path"]) for x in out] == [("Clients/Acme/Acme", "Clients/Acme/Acme.md"), ("Nowhere", None)]
    assert json.loads(one(tools, "wikilinks", {"query": "broken"})) == [
        {"source_path": "Clients/Acme/Calls/2026-10-01 Billing.md", "target": "Nowhere"}]
    assert json.loads(one(tools, "wikilinks", {"query": "orphans"})) == ["Personal/diary.md"]
    tagged = json.loads(one(tools, "search_metadata", {"type": "tag", "tag": "billing"}))
    assert [t["path"] for t in tagged] == ["Clients/Acme/Calls/2026-10-01 Billing.md"]
    assert json.loads(one(tools, "search_metadata", {"type": "tag", "tag": "billing", "include_nested": False})) == []
    calls = json.loads(one(tools, "search_metadata", {"type": "frontmatter", "field": "type", "value": "call"}))
    assert [c["path"] for c in calls] == ["Clients/Acme/Calls/2026-10-01 Billing.md"]
    assert len(json.loads(one(tools, "search_metadata", {"type": "frontmatter", "field": "type",
                                                         "operator": "exists"}))) == 2


def test_search_by_meaning_with_and_without_the_embedding_server(tools, vault, fake_embedder, monkeypatch):
    plain = json.loads(one(tools, "search_semantic", {"query": "payment"}))
    assert plain["mode"] == "keyword" and "off or not downloaded" in plain["message"]
    monkeypatch.setenv("EMBEDDING_BASE_URL", fake_embedder.url)
    monkeypatch.setenv("EMBEDDING_MODEL", "fake")
    model = embeddings.model_id(tools.home, tools.cat)
    client = EmbeddingClient(fake_embedder.url, "fake")
    for note in tools.fresh(force=True).to_embed(model, 50):
        pieces = embeddings.chunk_note(note["title"], note["body"])
        tools.index.put_vectors(note, model, pieces, client.embed([embeddings.embed_input(note["title"], h, t)
                                                                   for h, t in pieces]))
    found = json.loads(one(tools, "search_semantic", {"query": "payment", "top_k": 2}))
    assert found["mode"] == "semantic" and found["results"][0]["path"].endswith("Billing.md")
    assert found["results"][0]["heading"] == "Summary" and "content" not in found["results"][0]
    hybrid = json.loads(one(tools, "search_semantic", {"query": "september payment", "lexical_prefetch": True,
                                                       "include_content": True}))
    assert hybrid["mode"] == "hybrid" and [r["path"] for r in hybrid["results"]] == [
        "Clients/Acme/Calls/2026-10-01 Billing.md"] and hybrid["results"][0]["content"].startswith("---")
    fake_embedder.shutdown()
    fake_embedder.server_close()
    gone = json.loads(one(tools, "search_semantic", {"query": "invoice"}))
    assert gone["mode"] == "keyword" and "did not answer" in gone["message"] and gone["results"]


def test_create_and_patch_by_heading_with_backups(tools, vault):
    assert one(tools, "note_create", {"path": "Clients/Acme/Work/2026-10-02 Plan", "content": "# Plan\n",
                                      "frontmatter": {"type": "work"}}) == "Created Clients/Acme/Work/2026-10-02 Plan.md"
    assert (vault / "Clients/Acme/Work/2026-10-02 Plan.md").read_text() == "---\ntype: work\n---\n# Plan\n"
    error, text = run(tools, ("note_create", {"path": "Clients/Acme/Acme.md", "content": "gone"}))[0]
    assert error and "already exists" in text and "Snapshot" in (vault / "Clients/Acme/Acme.md").read_text()
    profile = vault / "Clients/Acme/Acme.md"
    one(tools, "note_patch", {"path": "Clients/Acme/Acme.md", "operation": "append", "target_type": "heading",
                              "target": "## Work Log", "content": "- 2026-10-02 — wrote the plan ([[Plan]])"})
    text = profile.read_text()
    assert "## Work Log\n- YYYY-MM-DD — what was done (link)\n- 2026-10-02 — wrote the plan ([[Plan]])\n" in text
    one(tools, "note_patch", {"path": "Clients/Acme/Acme.md", "operation": "prepend", "target_type": "heading",
                              "target": "Open Items", "content": "- [ ] Call back — me — Monday"})
    assert "## Open Items\n- [ ] Call back — me — Monday\n- [ ] Send revised proposal" in profile.read_text()
    one(tools, "note_patch", {"path": "Clients/Acme/Acme.md", "operation": "replace", "target_type": "heading",
                              "target": "Preferences & Watch-outs", "content": "Likes short emails."})
    assert "## Preferences & Watch-outs\nLikes short emails.\n\n## Work Log" in profile.read_text()
    one(tools, "note_patch", {"path": "Clients/Acme/Acme.md", "operation": "replace", "target_type": "frontmatter",
                              "target": "status", "content": "paused"})
    assert "status: paused\n" in profile.read_text()
    backups = sorted((vault / "_System/backups").iterdir())
    assert len(backups) == 4 and all(b.name.startswith("Acme ") for b in backups)
    assert "Likes short emails" not in backups[0].read_text()         # the version before the first patch
    error, text = run(tools, ("note_patch", {"path": "Clients/Acme/Acme.md", "operation": "append",
                                             "target_type": "heading", "target": "Nope", "content": "x"}))[0]
    assert error and "No heading 'Nope'" in text and "Work Log" in text
    # the search sees the change at once
    assert json.loads(one(tools, "search_text", {"query": "short emails"}))[0]["path"] == "Clients/Acme/Acme.md"


def test_write_and_frontmatter_keep_a_backup(tools, vault):
    note = vault / "Personal/diary.md"
    assert "(the old version is in _System/backups)" in one(tools, "note_write", {"path": "Personal/diary.md",
                                                                                   "content": "# Diary\n"})
    assert note.read_text() == "# Diary\n" and [b.read_text() for b in (vault / "_System/backups").iterdir()] == ["private"]
    error, text = run(tools, ("note_write", {"path": "Personal/new.md", "content": "x"}))[0]
    assert error and "There is no note" in text and not (vault / "Personal/new.md").exists()
    one(tools, "frontmatter", {"action": "set", "path": "Personal/diary.md", "key": "tags", "value": ["a", "b"]})
    assert json.loads(one(tools, "frontmatter", {"action": "get", "path": "Personal/diary.md"})) == {"tags": ["a", "b"]}
    one(tools, "frontmatter", {"action": "remove", "path": "Personal/diary.md", "key": "tags"})
    assert json.loads(one(tools, "frontmatter", {"action": "get", "path": "Personal/diary.md"})) is None
    assert note.read_text() == "# Diary\n"
    error, text = run(tools, ("frontmatter", {"action": "set", "path": "Personal/diary.md", "key": "a: b", "value": 1}))[0]
    assert error and "not a frontmatter key" in text


@pytest.mark.parametrize("path", ["../outside.md", "/etc/passwd", "C:/Windows/win.ini", "~/notes.md",
                                  ".obsidian/app.md", "00_Inbox/waiting.md", "_System/backups/x.md",
                                  "Clients/../../outside.md", "Clients/Acme/logo.png"])
def test_paths_outside_the_vault_or_in_closed_folders_are_refused(tools, vault, path):
    results = run(tools, ("note_read", {"path": path}), ("note_create", {"path": path, "content": "x"}),
                  ("note_write", {"path": path, "content": "x"}),
                  ("note_patch", {"path": path, "operation": "append", "target_type": "heading", "target": "A",
                                  "content": "x"}))
    assert all(error for error, _text in results), results
    assert not (vault.parent / "outside.md").exists() and (vault / "00_Inbox/waiting.md").read_text() == "not yet"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")
def test_a_link_that_leads_out_of_the_vault_is_refused(tools, vault, tmp_path):
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    (vault / "Personal/link.md").symlink_to(tmp_path / "secret.md")
    error, text = run(tools, ("note_read", {"path": "Personal/link.md"}))[0]
    assert error and "outside the vault" in text


def test_nothing_is_ever_deleted(tools, vault):
    before = {p for p in vault.rglob("*") if p.is_file()}
    run(tools, ("note_write", {"path": "Clients/Acme/Acme.md", "content": "short"}),
        ("note_patch", {"path": "Clients/Acme/Acme.md", "operation": "replace", "target_type": "heading",
                        "target": "Snapshot", "content": "x"}),
        ("frontmatter", {"action": "remove", "path": "Clients/Acme/Acme.md", "key": "type"}))
    assert before <= {p for p in vault.rglob("*") if p.is_file()}


def test_check_opens_the_vault_and_exits(vault, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(f"VAULT_PATH={vault}\n", encoding="utf-8")
    monkeypatch.setenv("VAULT_INGEST_HOME", str(home))
    monkeypatch.delenv("VAULT_PATH", raising=False)
    assert mcp_server.main(["--check"]) == 0
    assert capsys.readouterr().out.startswith("ok: 2 notes in ")
    assert (home / "index.sqlite").is_file()
