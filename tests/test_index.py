import os
import sqlite3
import time
from pathlib import Path

import pytest

from vault_ingest import index as index_module
from vault_ingest.index import Lookup, NoteIndex, fold, parse_links, snippet


def write(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def touch_later(path: Path) -> None:
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))             # a new mtime even on coarse file-system clocks


@pytest.fixture
def notes(tmp_path):
    vault = tmp_path / "Vault"
    write(vault, "Clients/Šumar/Šumar.md", "---\ntype: client\naliases: [Sumar d.o.o., Forest]\n---\n# Šumar\n\n"
          "## Current Status\nWaiting on Đorđe.\n\n## Open Items\n- [ ] Send the offer — me — Friday\n"
          "- [x] Old item\n- [ ] Book the room\n")
    write(vault, "Clients/Šumar/Calls/2026-10-01 Kickoff.md",
          "---\ntype: call\nclient: Šumar\ndate: 2026-10-01\n---\n# Kickoff\n\n**Client:** [[Clients/Šumar/Šumar|Šumar]]\n\n"
          "## Summary\nČetvrtak je dan za ćevape i žgance. Staging link goes out Friday.\n\n"
          "## Transcript\n- [[Clients/Šumar/Transcripts/2026-10-01 Kickoff/2026-10-01 Kickoff - Part 01|Part 1]]\n")
    write(vault, "Clients/Šumar/Transcripts/2026-10-01 Kickoff/2026-10-01 Kickoff - Part 01.md",
          '---\ntype: transcript-chunk\ncall: "[[Clients/Šumar/Calls/2026-10-01 Kickoff]]"\n---\nThe staging link.\n')
    write(vault, "Work/Ideas.md", "# Ideas\n\nSee [[Kickoff#Summary|the kickoff]], ![[Forest]] and [[Nowhere]].\n"
          "![[diagram.png]] `[[Not a link]]`\n```\n[[Also not]]\n```\n")
    write(vault, "_System/Templates/Client.md", "---\ntype: client\n---\n# {client}\n")
    write(vault, "00_Inbox/_needs-review/x.why.md", "# why\n")
    write(vault, "Clients/Šumar/Transcripts/raw/raw.md", "raw\n")
    write(vault, ".obsidian/workspace.md", "x\n")
    idx = NoteIndex(vault, tmp_path / "home" / "index.sqlite")
    idx.refresh()
    yield vault, idx
    idx.close()


def paths(idx):
    return sorted(r["path"] for r in idx._rows("SELECT path FROM notes"))


def test_build_skips_system_inbox_raw_and_obsidian_folders(notes):
    vault, idx = notes
    assert paths(idx) == ["Clients/Šumar/Calls/2026-10-01 Kickoff.md",
                          "Clients/Šumar/Transcripts/2026-10-01 Kickoff/2026-10-01 Kickoff - Part 01.md",
                          "Clients/Šumar/Šumar.md", "Work/Ideas.md"]
    assert idx.note("Clients/Šumar/Šumar.md") | {"id": 0} == {
        "id": 0, "path": "Clients/Šumar/Šumar.md", "title": "Šumar", "type": "client", "client": "Šumar", "date": None}
    assert idx.refresh() == []                                  # nothing changed: nothing done
    assert not (vault / "index.sqlite").exists() and not list(vault.rglob("*.sqlite"))


def test_update_keeps_the_id_and_delete_removes_links_and_text(notes):
    vault, idx = notes
    before = idx.note("Work/Ideas.md")["id"]
    ideas = write(vault, "Work/Ideas.md", "# Ideas\n\nNow about [[Šumar]] and pelmeni.\n")
    touch_later(ideas)
    assert idx.refresh() == ["Work/Ideas.md"]
    assert idx.note("Work/Ideas.md")["id"] == before
    assert [r["path"] for r in idx.search("pelmeni")] == ["Work/Ideas.md"]
    assert idx.search("nowhere") == []
    assert [b["path"] for b in idx.backlinks("Clients/Šumar/Šumar.md")] == [
        "Clients/Šumar/Calls/2026-10-01 Kickoff.md", "Work/Ideas.md"]

    ideas.unlink()
    assert idx.refresh() == ["Work/Ideas.md"]
    assert idx.note("Work/Ideas.md") is None and idx.search("pelmeni") == []
    assert [b["path"] for b in idx.backlinks("Clients/Šumar/Šumar.md")] == ["Clients/Šumar/Calls/2026-10-01 Kickoff.md"]
    with idx._lock:
        assert idx.db.execute("SELECT count(*) FROM links WHERE source = ?", (before,)).fetchone() == (0,)


def test_a_new_note_resolves_links_that_were_missing(notes):
    vault, idx = notes
    graph = idx.graph(missing=True)
    assert "missing:nowhere" in {n["id"] for n in graph["nodes"]}
    write(vault, "Archive/Nowhere.md", "# here now\n")
    idx.refresh()
    graph = idx.graph(missing=True)
    assert "missing:nowhere" not in {n["id"] for n in graph["nodes"]}
    assert {"source": "Work/Ideas.md", "target": "Archive/Nowhere.md"} in graph["edges"]


def test_link_parsing_aliases_headings_embeds_code(notes):
    links = parse_links((notes[0] / "Work/Ideas.md").read_text(encoding="utf-8"))
    assert [(x["target"], x["heading"], x["alias"], x["embed"]) for x in links] == [
        ("Kickoff", "Summary", "the kickoff", False), ("Forest", None, None, True), ("Nowhere", None, None, False),
        ("diagram.png", None, None, True)]
    assert links[0]["context"].startswith("See [[Kickoff#Summary")
    assert parse_links("[[#Only a heading]] and [[a\\|b]]")[0]["target"] == "a"       # the table form


def test_link_resolution_edge_cases(notes):
    vault, idx = notes
    resolved = {x["target"]: x["path"] for x in idx.links((vault / "Work/Ideas.md").read_text(encoding="utf-8"),
                                                          "Work/Ideas.md")}
    assert resolved == {"Kickoff": None, "Forest": "Clients/Šumar/Šumar.md", "Nowhere": None, "diagram.png": None}
    # "Kickoff" is the file name 2026-10-01 Kickoff, not Kickoff: missing, as in Obsidian
    lookup = Lookup([("A/Note.md", []), ("B/Note.md", []), ("B/Deep/Note.md", []), ("C/Other.md", ["Nick"])])
    assert lookup.resolve("note", "B/x.md") == "B/Note.md"                 # same folder wins
    assert lookup.resolve("Note", "Z/x.md") == "A/Note.md"                 # else the shortest path
    assert lookup.resolve("Deep/Note", "Z/x.md") == "B/Deep/Note.md"       # a path ending
    assert lookup.resolve("../C/Other", "B/x.md") == "C/Other.md"          # relative to the linking note
    assert lookup.resolve("C/Other.md", "x.md") == "C/Other.md"
    assert lookup.resolve("nick", "x.md") == "C/Other.md"                  # a frontmatter alias
    assert lookup.resolve("Other.png", "x.md") is None and lookup.resolve("Missing", "x.md") is None


def test_backlinks_carry_the_line_around_the_link(notes):
    _, idx = notes
    [back] = idx.backlinks("Clients/Šumar/Calls/2026-10-01 Kickoff.md")
    assert back["path"] == "Clients/Šumar/Transcripts/2026-10-01 Kickoff/2026-10-01 Kickoff - Part 01.md"
    assert back["type"] == "transcript-chunk" and back["contexts"] == ['call: "[[Clients/Šumar/Calls/2026-10-01 Kickoff]]"']


@pytest.mark.parametrize("query", ["cetvrtak", "ČETVRTAK", "cevape", "zgance", "dorde", "djordje", "Đorđe", "četv"])
def test_search_ignores_case_and_diacritics(notes, query):
    _, idx = notes
    hits = idx.search(query)
    assert hits, query
    assert any(piece[1] for piece in hits[0]["snippet"])


def test_search_ranks_titles_and_aliases_first_and_filters_types(notes):
    _, idx = notes
    assert idx.search("sumar")[0]["path"] == "Clients/Šumar/Šumar.md"
    assert idx.search("forest")[0]["path"] == "Clients/Šumar/Šumar.md"
    assert {r["type"] for r in idx.search("staging")} == {"call", "transcript-chunk"}
    assert [r["type"] for r in idx.search("staging", types=("transcript-chunk",))] == ["transcript-chunk"]
    assert [r["path"] for r in idx.search("ideas", types=("other",))] == ["Work/Ideas.md"]
    assert idx.search("") == [] and idx.search('"*)(:') == []          # FTS syntax in the query is just text


def test_snippet_keeps_the_original_letters_around_a_folded_hit():
    pieces = snippet("Prvo nešto.\n\n" + "riječ " * 60 + "Četvrtak je dan, a **Đorđe** dolazi.", ["cetvrtak", "dorde"])
    hits = [text for text, hit in pieces if hit]
    assert hits == ["Četvrtak", "Đorđe"]
    assert pieces[0] == ["…", False] and "**" not in "".join(t for t, _ in pieces)


def test_fold_is_one_character_for_one():
    text = "Čćđšž ŠĐ ß İ ﬁ"
    assert len(fold(text)) == len(text) and fold("Čćđšž") == "ccdsz"


def test_graph_defaults_filters_and_degrees(notes):
    _, idx = notes
    graph = idx.graph()
    assert {n["type"] for n in graph["nodes"]} == {"client", "call", "other"}
    assert {(e["source"], e["target"]) for e in graph["edges"]} == {
        ("Clients/Šumar/Calls/2026-10-01 Kickoff.md", "Clients/Šumar/Šumar.md"),
        ("Work/Ideas.md", "Clients/Šumar/Šumar.md")}
    profile = next(n for n in graph["nodes"] if n["type"] == "client")
    assert profile["links"] == 2
    with_parts = idx.graph(("call", "transcript-chunk"), ["Šumar"])
    assert {n["type"] for n in with_parts["nodes"]} == {"call", "transcript-chunk"}
    assert len(with_parts["edges"]) == 2                       # call -> part and part -> call
    assert idx.graph(clients=["Nobody"]) == {"nodes": [], "edges": []}


def test_client_stats(notes):
    _, idx = notes
    assert idx.client_stats() == {"Šumar": {"meetings": 1, "last_meeting": "2026-10-01", "open_items": 2,
                                            "profile": "Clients/Šumar/Šumar.md"}}


def test_a_moved_vault_is_indexed_from_scratch(notes, tmp_path):
    vault, idx = notes
    idx.close()
    other = tmp_path / "Other"
    write(other, "One.md", "# one\n")
    again = NoteIndex(other, idx.db_path)
    again.refresh()
    assert paths(again) == ["One.md"]
    again.close()


def test_without_fts5_search_still_works(tmp_path, monkeypatch):
    real = sqlite3.connect

    class NoFts:
        def __init__(self, *a, **k):
            self.conn = real(*a, **k)

        def execute(self, sql, *args):
            if "fts5" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __enter__(self):
            return self.conn.__enter__()

        def __exit__(self, *exc):
            return self.conn.__exit__(*exc)

    monkeypatch.setattr(index_module.sqlite3, "connect", NoFts)
    vault = tmp_path / "Vault"
    write(vault, "Notes/Četvrtak.md", "# Četvrtak\n\nĐorđe dolazi.\n")
    write(vault, "Notes/Other.md", "# Other\n\nO četvrtku.\n")
    idx = NoteIndex(vault, tmp_path / "index.sqlite")
    idx.refresh()
    assert idx.fts is False
    assert [r["path"] for r in idx.search("cetvrt")] == ["Notes/Četvrtak.md", "Notes/Other.md"]
    assert [r["path"] for r in idx.search("dorde")] == ["Notes/Četvrtak.md"]
    idx.close()


def test_the_watcher_updates_the_index_and_reports_paths(notes, monkeypatch):
    vault, idx = notes
    monkeypatch.setattr(index_module, "DEBOUNCE_SECONDS", 0.2)
    seen = []
    idx.watch(seen.append)
    time.sleep(0.3)
    write(vault, "Work/New.md", "# New\n\nfresh words\n")
    for _ in range(50):
        if seen:
            break
        time.sleep(0.1)
    assert seen and "Work/New.md" in seen[0]
    assert [r["path"] for r in idx.search("fresh")] == ["Work/New.md"]
