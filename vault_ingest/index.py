"""The note index: one SQLite file in the app's data folder with every note of the vault, the [[links]] between
them and a full-text index over title and body. It only reads the vault and can be rebuilt from the files at any
time, so a synced vault never carries a database.

Search ignores case and diacritics: text is folded in Python before it is indexed (č ć → c, š → s, ž → z, đ → d),
because SQLite's `remove_diacritics` leaves đ alone. Where the bundled SQLite has no FTS5, the same table is a plain
one searched with LIKE (slower, no ranking), and `NoteIndex.fts` is False.

Search by meaning: `chunks` holds one vector per passage of a note and `embedded` which note version and model they
came from, both keyed by `notes.id` (ids stay the same while a note is updated in place). Only the engine writes
vectors; a reader (the MCP server) embeds just the query. Vectors are compared with numpy, all in memory.

Two processes may open the file (the engine and an MCP server per AI tool): WAL lets them read while one writes,
and a refresh takes the write lock before it looks at what changed, so two refreshes never do the same work twice.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import posixpath
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Callable

from .vault import get_frontmatter, get_section, open_items, split_frontmatter

log = logging.getLogger("vault_ingest")
SCHEMA = 1
# never indexed: app state, the inbox, Obsidian's own folders; raw transcripts are not notes
SKIP_DIRS = {".obsidian", ".trash", ".git", "_System", "00_Inbox"}
SKIP_PARTS = ("Transcripts/raw/",)
ATTACHMENT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".pdf", ".mp3", ".mp4", ".m4a", ".wav",
              ".ogg", ".opus", ".webm", ".flac", ".txt", ".docx", ".xlsx", ".pptx", ".canvas", ".base", ".zip"}
NOTE_TYPES = ("client", "call", "transcript-chunk")          # everything else is "other"
DEFAULT_GRAPH_TYPES = ("client", "call", "other")
WIKILINK = re.compile(r"(!?)\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]")
CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
TERM = re.compile(r"\w+")
DEBOUNCE_SECONDS = 1.0
PREFETCH = 200                 # keyword candidates that search by meaning reranks with `lexical`


@functools.lru_cache(maxsize=4096)
def _fold_char(c: str) -> str:
    if c in "đĐ":
        return "d"
    base = unicodedata.normalize("NFD", c)[0].lower()
    return base if len(base) == 1 else c                       # one character for one, so offsets carry over


def fold(text: str) -> str:
    """Lower case without diacritics, the same length as `text`."""
    return "".join(_fold_char(c) for c in text)


def note_type(raw) -> str:
    raw = str(raw or "")
    return raw if raw in NOTE_TYPES else "other"


def parse_links(text: str) -> list[dict]:
    """Every [[link]] and ![[embed]] outside code, frontmatter included (Obsidian counts property links)."""
    out = []
    plain = CODE.sub(lambda m: " " * len(m.group()), text)
    for m in WIKILINK.finditer(plain):
        bang, inner, alias = m.groups()
        target, _, heading = inner.partition("#")
        target = target.strip().rstrip("\\")                    # `[[a\|b]]` inside a table
        if not target:
            continue                                            # `[[#Heading]]`: a link inside the same note
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        line = text[start:end if end >= 0 else len(text)].strip()
        out.append({"target": target, "heading": heading.strip() or None, "alias": (alias or "").strip() or None,
                    "embed": bool(bang), "context": _context(line, m.start() - start)})
    return out


def _context(line: str, at: int, width: int = 200) -> str:
    line = re.sub(r"^(?:[-*+]\s+|>\s*|\d+\.\s+|#+\s+|\[[ xX]\]\s*)+", "", line)
    if len(line) <= width:
        return line
    begin = max(0, min(at - width // 2, len(line) - width))
    return ("…" if begin else "") + line[begin:begin + width].strip() + ("…" if begin + width < len(line) else "")


def is_attachment(target: str) -> bool:
    return posixpath.splitext(target)[1].lower() in ATTACHMENT


class Lookup:
    """Obsidian's link resolution over the indexed paths: exact vault path, path relative to the linking note,
    path ending, unique file name, then (beyond Obsidian) a frontmatter alias. Ties go to the note in the same
    folder, else the shortest path. Case does not matter."""

    def __init__(self, notes: list[tuple[str, list[str]]]):
        self.by_path: dict[str, str] = {}
        self.by_name: dict[str, list[str]] = {}
        self.by_alias: dict[str, list[str]] = {}
        for path, aliases in notes:
            key = path[:-3].lower()
            self.by_path[key] = path
            self.by_name.setdefault(posixpath.basename(key), []).append(path)
            for a in aliases:
                self.by_alias.setdefault(a.lower(), []).append(path)

    def resolve(self, target: str, source: str) -> str | None:
        t = target.replace("\\", "/").strip().lstrip("/")
        if is_attachment(t):
            return None
        key = (t[:-3] if t.lower().endswith(".md") else t).lower()
        if not key:
            return None
        if key in self.by_path:
            return self.by_path[key]
        near = posixpath.normpath(posixpath.join(posixpath.dirname(source.lower()), key))
        if near in self.by_path:
            return self.by_path[near]
        names = self.by_name.get(posixpath.basename(key), [])
        if "/" in key:
            names = [p for p in names if p[:-3].lower().endswith("/" + key)]
        return self._pick(names, source) or self._pick(self.by_alias.get(key, []), source)

    @staticmethod
    def _pick(paths: list[str], source: str) -> str | None:
        if len(paths) < 2:
            return paths[0] if paths else None
        folder = posixpath.dirname(source)
        return min(paths, key=lambda p: (posixpath.dirname(p) != folder, p.count("/"), len(p), p))


def _aliases(fm: dict) -> list[str]:
    raw = fm.get("aliases") or fm.get("alias") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(a).strip() for a in raw if str(a).strip()] if isinstance(raw, list) else []


def _plain(body: str) -> str:
    """The body as one line of readable text, for snippets (without the note's own `# Title`)."""
    body = re.sub(r"\A\s*# [^\n]*\n", "", body)
    body = WIKILINK.sub(lambda m: m.group(3) or posixpath.basename(m.group(2).partition("#")[0]), body)
    body = re.sub(r"[*`#>]+|^---$", " ", body, flags=re.M)
    return re.sub(r"\s+", " ", body).strip()


def snippet(body: str, terms: list[str], width: int = 180) -> list[list]:
    """A window of the body around the first hit, as [text, is_hit] pieces; terms match as word prefixes."""
    text = _plain(body)
    folded = fold(text)
    words = sorted({w for t in terms for w in (t, t.replace("dj", "d")) if w}, key=len, reverse=True)
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + r")\w*") if words else None
    first = pattern.search(folded) if pattern else None
    start = 0
    if first and first.start() > width // 3:
        start = folded.rfind(" ", 0, first.start() - width // 3) + 1
    end = min(len(text), start + width)
    if end < len(text):
        end = max(text.rfind(" ", start, end), start + width // 2)
    pieces: list[list] = [["…", False]] if start else []
    at = start
    for m in (pattern.finditer(folded, start, end) if pattern else ()):
        if m.start() > at:
            pieces.append([text[at:m.start()], False])
        pieces.append([text[m.start():min(m.end(), end)], True])
        at = min(m.end(), end)
    if at < end:
        pieces.append([text[at:end], False])
    if end < len(text):
        pieces.append(["…", False])
    return pieces


def _fts_query(terms: list[str], any_term: bool = False) -> str:
    def one(t: str, last: bool) -> str:
        star = "*" if last else ""
        forms = [f'"{t}"{star}'] + ([f'"{t.replace("dj", "d")}"{star}'] if "dj" in t else [])   # dj typed for đ
        return forms[0] if len(forms) == 1 else f"({' OR '.join(forms)})"

    return (" OR " if any_term else " ").join(one(t, i == len(terms) - 1) for i, t in enumerate(terms))


class NoteIndex:
    def __init__(self, vault_root: Path, db_path: Path):
        self.root = Path(vault_root).resolve()
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.fts = self._schema()
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._observer = None
        self._thread: threading.Thread | None = None
        self._lookup: Lookup | None = None
        self._vectors_cache: tuple | None = None

    # ---------------------------------------------------------------- schema
    def _schema(self) -> bool:
        with self._lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            meta = dict(self.db.execute("SELECT key, value FROM meta"))
            if meta.get("vault") != str(self.root) or meta.get("schema") != str(SCHEMA):
                for table in ("links", "notes_fts", "chunks", "embedded", "notes"):
                    self.db.execute(f"DROP TABLE IF EXISTS {table}")
            self.db.execute("""CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, title TEXT, type TEXT, client TEXT, date TEXT,
                aliases TEXT, mtime REAL, size INTEGER, body TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS links (
                source INTEGER NOT NULL, target_text TEXT, heading TEXT, alias TEXT, embed INTEGER, context TEXT,
                target INTEGER)""")
            self.db.execute("CREATE INDEX IF NOT EXISTS links_source ON links(source)")
            self.db.execute("CREATE INDEX IF NOT EXISTS links_target ON links(target)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS chunks (
                note INTEGER NOT NULL, seq INTEGER NOT NULL, heading TEXT, text TEXT, vector BLOB NOT NULL,
                PRIMARY KEY (note, seq))""")
            self.db.execute("CREATE TABLE IF NOT EXISTS embedded (note INTEGER PRIMARY KEY, model TEXT, mtime REAL, size INTEGER)")
            try:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
                                "title, body, tokenize='unicode61 remove_diacritics 2')")
                fts = True
            except sqlite3.OperationalError:
                log.warning("This SQLite has no FTS5; note search falls back to plain matching.")
                self.db.execute("CREATE TABLE IF NOT EXISTS notes_fts (rowid INTEGER PRIMARY KEY, title TEXT, body TEXT)")
                fts = False
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                                [("vault", str(self.root)), ("schema", str(SCHEMA))])
        return fts

    # ---------------------------------------------------------------- building
    def _files(self) -> dict[str, os.stat_result]:
        found = {}
        for folder, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if not name.lower().endswith(".md") or name.startswith("."):
                    continue
                full = Path(folder) / name
                rel = full.relative_to(self.root).as_posix()
                if any(part in rel + "/" for part in SKIP_PARTS):
                    continue
                try:
                    found[rel] = full.stat()
                except OSError:
                    continue
        return found

    def refresh(self) -> list[str]:
        """Bring the index up to date with the files (by modified time and size). Returns the paths that changed."""
        files = self._files()
        with self._lock:
            _, changed, gone = self._diff(files)
            if not changed and not gone:
                return []
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")              # the write lock first, then what changed under it
                known, changed, gone = self._diff(files)
                for path in gone:
                    self._delete(known[path][0])
                for path in changed:
                    self._put(path, files[path], known.get(path, (None,))[0])
                if changed or gone:                             # another process may have done it meanwhile
                    self._resolve_all()
        return sorted(changed + gone)

    def _diff(self, files: dict[str, os.stat_result]) -> tuple[dict, list[str], list[str]]:
        known = {p: (i, m, s) for i, p, m, s in self.db.execute("SELECT id, path, mtime, size FROM notes")}
        changed = [p for p, st in files.items() if known.get(p, (0, None, None))[1:] != (st.st_mtime, st.st_size)]
        return known, changed, [p for p in known if p not in files]

    def _delete(self, note_id: int) -> None:
        self.db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.db.execute("DELETE FROM links WHERE source = ?", (note_id,))
        self.db.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))
        self.db.execute("DELETE FROM chunks WHERE note = ?", (note_id,))
        self.db.execute("DELETE FROM embedded WHERE note = ?", (note_id,))
        self._bump()

    def _bump(self) -> None:
        """Tell every process that the stored vectors changed (they cache them in memory)."""
        self.db.execute("INSERT INTO meta VALUES ('vectors', '1') "
                        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")

    def _put(self, path: str, st: os.stat_result, note_id: int | None) -> None:
        try:
            text = (self.root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""                                           # kept as an empty note: links to it still resolve
        fm = get_frontmatter(text)
        body = split_frontmatter(text)[1]
        parts = path.split("/")
        client = parts[1] if len(parts) > 2 and parts[0] == "Clients" else str(fm.get("client") or "") or None
        title = posixpath.basename(path)[:-3]
        aliases = _aliases(fm)
        row = (title, str(fm.get("type") or ""), client, str(fm.get("date") or "")[:10] or None,
               json.dumps(aliases, ensure_ascii=False), st.st_mtime, st.st_size, body)
        if note_id is None:
            note_id = self.db.execute("INSERT INTO notes (title, type, client, date, aliases, mtime, size, body, path) "
                                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*row, path)).lastrowid
        else:
            self.db.execute("UPDATE notes SET title=?, type=?, client=?, date=?, aliases=?, mtime=?, size=?, body=? "
                            "WHERE id=?", (*row, note_id))
            self.db.execute("DELETE FROM links WHERE source = ?", (note_id,))
            self.db.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))
        self.db.execute("INSERT INTO notes_fts (rowid, title, body) VALUES (?, ?, ?)",
                        (note_id, fold(" ".join([title, *aliases])), fold(body)))
        self.db.executemany("INSERT INTO links (source, target_text, heading, alias, embed, context) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            [(note_id, x["target"], x["heading"], x["alias"], int(x["embed"]), x["context"])
                             for x in parse_links(text)])

    def _resolve_all(self) -> None:
        # a new or removed note can change where any link points, so every link is resolved again (cheap: dict lookups)
        notes = list(self.db.execute("SELECT id, path, aliases FROM notes"))
        ids = {path: i for i, path, _ in notes}
        lookup = self._lookup = Lookup([(path, json.loads(a or "[]")) for _, path, a in notes])
        paths = {i: path for i, path, _ in notes}
        rows = self.db.execute("SELECT rowid, source, target_text FROM links").fetchall()
        self.db.executemany("UPDATE links SET target = ? WHERE rowid = ?",
                            [(ids.get(lookup.resolve(t, paths[s])), rowid) for rowid, s, t in rows])

    # ---------------------------------------------------------------- watching
    def watch(self, on_change: Callable[[list[str]], None]) -> None:
        """Keep the index current from file events (watchdog), batched for DEBOUNCE_SECONDS."""
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        dirty = self._dirty

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                paths = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
                if event.is_directory or any(str(p).lower().endswith(".md") for p in paths):
                    dirty.set()

        def loop() -> None:
            while not self._stop.is_set():
                dirty.wait()
                if self._stop.wait(DEBOUNCE_SECONDS):
                    break
                dirty.clear()
                try:
                    changed = self.refresh()
                    if changed:
                        on_change(changed)
                except Exception:  # noqa: BLE001
                    log.exception("Updating the note index failed")

        self._observer = Observer()
        self._observer.schedule(Handler(), str(self.root), recursive=True)
        self._observer.start()
        self._thread = threading.Thread(target=loop, name="note-index", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._dirty.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            self.db.close()

    # ---------------------------------------------------------------- reading
    def _rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            cur = self.db.execute(sql, args)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def search(self, query: str, limit: int = 20, types: tuple[str, ...] | None = None,
               any_term: bool = False) -> list[dict]:
        """Notes whose title, aliases or text contain every word of `query` (the last one as a prefix), best first;
        with `any_term`, at least one of the words."""
        terms = TERM.findall(fold(query))[:12]
        if not terms:
            return []
        where, args = "", []
        if types:
            where, args = self._type_filter(types)
            where = " AND " + where
        cols = "n.id, n.path, n.title, n.type, n.client, n.date, n.body"
        if self.fts:
            rows = self._rows(f"SELECT {cols} FROM notes_fts f JOIN notes n ON n.id = f.rowid WHERE notes_fts MATCH ?"
                              f"{where} ORDER BY bm25(notes_fts, 10.0, 1.0) LIMIT ?",
                              [_fts_query(terms, any_term), *args, limit])
        else:
            like = (" OR " if any_term else " AND ").join("(f.title LIKE ? OR f.body LIKE ?)" for _ in terms)
            likes = [x for t in terms for x in (f"%{t}%", f"%{t}%")]
            rows = self._rows(f"SELECT {cols} FROM notes_fts f JOIN notes n ON n.id = f.rowid WHERE {like}{where} "
                              f"ORDER BY f.title LIKE ? DESC, n.date DESC LIMIT ?", [*likes, *args, f"%{terms[0]}%", limit])
        return [{"path": r["path"], "title": r["title"], "type": note_type(r["type"]), "client": r["client"],
                 "date": r["date"], "snippet": snippet(r["body"] or "", terms)} for r in rows]

    # ---------------------------------------------------------------- search by meaning
    def to_embed(self, model: str, limit: int = 8) -> list[dict]:
        """Notes without vectors from `model` for their current version; the newest notes first."""
        return self._rows("SELECT n.id, n.path, n.title, n.body, n.mtime, n.size FROM notes n "
                          "LEFT JOIN embedded e ON e.note = n.id WHERE e.note IS NULL OR e.model != ? "
                          "OR e.mtime != n.mtime OR e.size != n.size "
                          "ORDER BY n.date IS NULL, n.date DESC, n.mtime DESC LIMIT ?", (model, limit))

    def embedded_count(self, model: str) -> dict:
        """{done, total}: notes whose current version has vectors from `model`, and all notes."""
        row = self._rows("SELECT count(*) AS total, sum(e.model = ? AND e.mtime = n.mtime AND e.size = n.size) AS done "
                         "FROM notes n LEFT JOIN embedded e ON e.note = n.id", (model,))[0]
        return {"done": int(row["done"] or 0), "total": int(row["total"])}

    def put_vectors(self, note: dict, model: str, pieces: list[tuple[str | None, str]], vectors) -> None:
        """Store the passages of `note` (a row from `to_embed`) with their vectors, replacing older ones."""
        import numpy as np

        vectors = np.asarray(vectors, dtype=np.float32).reshape(len(pieces), -1)
        with self._lock, self.db:
            if not self.db.execute("SELECT 1 FROM notes WHERE id = ?", (note["id"],)).fetchone():
                return                                          # deleted while it was being embedded
            self.db.execute("DELETE FROM chunks WHERE note = ?", (note["id"],))
            self.db.executemany("INSERT INTO chunks (note, seq, heading, text, vector) VALUES (?, ?, ?, ?, ?)",
                                [(note["id"], i, heading, text, vectors[i].tobytes())
                                 for i, (heading, text) in enumerate(pieces)])
            # the version that was read: a note changed meanwhile stays pending and is embedded again
            self.db.execute("INSERT OR REPLACE INTO embedded VALUES (?, ?, ?, ?)",
                            (note["id"], model, note["mtime"], note["size"]))
            self._bump()

    def _vectors(self, model: str):
        """(note ids, passage numbers, unit vectors as one matrix) of `model`, cached until the vectors change."""
        import numpy as np

        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = 'vectors'").fetchone()
            key = (row[0] if row else None, model)
            if self._vectors_cache is None or self._vectors_cache[0] != key:
                rows = self.db.execute("SELECT c.note, c.seq, c.vector FROM chunks c JOIN embedded e ON e.note = c.note "
                                       "WHERE e.model = ?", (model,)).fetchall()
                notes = np.array([r[0] for r in rows], dtype=np.int64)
                seqs = np.array([r[1] for r in rows], dtype=np.int64)
                matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1) \
                    if rows else np.zeros((0, 0), dtype=np.float32)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True) if rows else None
                if rows:
                    matrix = matrix / np.where(norms == 0, 1, norms)
                self._vectors_cache = (key, notes, seqs, matrix)
            return self._vectors_cache[1:]

    def _lexical(self, query: str, limit: int, types) -> dict[int, float]:
        """Keyword candidates for `query` (any of its words), as {note id: score from 0 to 1}."""
        terms = TERM.findall(fold(query))[:12]
        if not terms:
            return {}
        where, args = ("", [])
        if types:
            where, args = self._type_filter(types)
            where = " AND " + where
        if not self.fts:
            like = " OR ".join("(f.title LIKE ? OR f.body LIKE ?)" for _ in terms)
            rows = self._rows(f"SELECT n.id FROM notes_fts f JOIN notes n ON n.id = f.rowid WHERE ({like}){where} "
                              f"LIMIT ?", [x for t in terms for x in (f"%{t}%", f"%{t}%")] + [*args, limit])
            return {r["id"]: 1.0 for r in rows}
        rows = self._rows(f"SELECT n.id, -bm25(notes_fts, 10.0, 1.0) AS score FROM notes_fts f JOIN notes n "
                          f"ON n.id = f.rowid WHERE notes_fts MATCH ?{where} ORDER BY bm25(notes_fts, 10.0, 1.0) LIMIT ?",
                          [_fts_query(terms, any_term=True), *args, limit])
        top = max((r["score"] for r in rows), default=0) or 1.0
        return {r["id"]: max(0.0, r["score"]) / top for r in rows}

    def semantic(self, vector, model: str, limit: int = 10, *, query: str = "", lexical: bool = False,
                 alpha: float = 0.25, types: tuple[str, ...] | None = None) -> list[dict]:
        """Notes nearest to `vector` (the query embedded with `model`), each by its closest passage.

        With `lexical`, only the keyword candidates for `query` are ranked, by alpha * keyword score +
        (1 - alpha) * cosine, as the owner's tools use it ("lexical_prefetch")."""
        import numpy as np

        notes, seqs, matrix = self._vectors(model)
        if not len(notes):
            return []
        q = np.asarray(vector, dtype=np.float32).reshape(-1)
        if q.shape[0] != matrix.shape[1]:
            raise ValueError(f"the query has {q.shape[0]} dimensions, the stored vectors {matrix.shape[1]}")
        q = q / (np.linalg.norm(q) or 1.0)
        cosine = matrix @ q
        allowed = None
        if types:
            where, args = self._type_filter(types)
            allowed = {r["id"] for r in self._rows(f"SELECT n.id FROM notes n WHERE {where}", args)}
        keyword: dict[int, float] = {}
        if lexical:
            keyword = self._lexical(query, PREFETCH, types)
            allowed = set(keyword) if allowed is None else allowed & set(keyword)
        best: dict[int, tuple[float, int]] = {}
        for i in np.argsort(-cosine):
            nid = int(notes[i])
            if nid in best or (allowed is not None and nid not in allowed):
                continue
            best[nid] = (float(cosine[i]), int(seqs[i]))
            if not lexical and len(best) >= limit:
                break                                           # sorted by cosine: the rest cannot win
        alpha = min(1.0, max(0.0, float(alpha)))
        ranked = sorted(((alpha * keyword[n] + (1 - alpha) * c if lexical else c, n, c, seq)
                         for n, (c, seq) in best.items()), reverse=True)[:limit]
        terms = TERM.findall(fold(query))[:12]
        out = []
        for score, nid, cos, seq in ranked:
            rows = self._rows("SELECT n.path, n.title, n.type, n.client, n.date, c.heading, c.text FROM notes n "
                              "JOIN chunks c ON c.note = n.id AND c.seq = ? WHERE n.id = ?", (seq, nid))
            if not rows:
                continue                                        # removed since the vectors were cached
            r = rows[0]
            out.append({"path": r["path"], "title": r["title"], "type": note_type(r["type"]), "client": r["client"],
                        "date": r["date"], "score": round(score, 4), "cosine": round(cos, 4),
                        "heading": r["heading"], "passage": r["text"], "snippet": snippet(r["text"] or "", terms)})
        return out

    # ---------------------------------------------------------------- links for the MCP tools
    def broken(self, path: str | None = None) -> list[dict]:
        """Links that resolve to no note (attachments left out: the index does not track them)."""
        sql = "SELECT s.path, l.target_text FROM links l JOIN notes s ON s.id = l.source WHERE l.target IS NULL"
        rows = self._rows(sql + (" AND s.path = ?" if path else "") + " ORDER BY s.path, l.rowid",
                          (path,) if path else ())
        return [{"source_path": r["path"], "target": r["target_text"]} for r in rows if not is_attachment(r["target_text"])]

    def orphans(self) -> list[str]:
        """Notes with no resolved link in or out."""
        return [r["path"] for r in self._rows(
            "SELECT path FROM notes n WHERE NOT EXISTS (SELECT 1 FROM links WHERE source = n.id AND target IS NOT NULL) "
            "AND NOT EXISTS (SELECT 1 FROM links WHERE target = n.id AND source != n.id) ORDER BY path")]

    @staticmethod
    def _type_filter(types) -> tuple[str, list]:
        named = [t for t in types if t in NOTE_TYPES]
        parts = ([f"n.type IN ({','.join('?' * len(named))})"] if named else []) + \
                ([f"n.type NOT IN ({','.join('?' * len(NOTE_TYPES))})"] if "other" in types else [])
        args = named + (list(NOTE_TYPES) if "other" in types else [])
        return f"({' OR '.join(parts) or '0'})", args

    def note(self, path: str) -> dict | None:
        rows = self._rows("SELECT id, path, title, type, client, date FROM notes WHERE path = ?", (path,))
        return rows[0] if rows else None

    def links(self, text: str, path: str) -> list[dict]:
        """The links in `text` (the note at `path` as read now), in reading order, each with the note it
        resolves to (None: missing, or an attachment)."""
        with self._lock:
            if self._lookup is None:
                self._lookup = Lookup([(p, json.loads(a or "[]")) for p, a in
                                       self.db.execute("SELECT path, aliases FROM notes")])
            lookup = self._lookup
        return [{"target": x["target"], "heading": x["heading"], "alias": x["alias"], "embed": x["embed"],
                 "attachment": is_attachment(x["target"]), "path": lookup.resolve(x["target"], path)}
                for x in parse_links(text)]

    def backlinks(self, path: str) -> list[dict]:
        """Notes that link to `path`, newest first, each with the lines around its links."""
        rows = self._rows("SELECT s.path, s.title, s.type, s.client, s.date, l.context FROM links l "
                          "JOIN notes t ON t.id = l.target JOIN notes s ON s.id = l.source "
                          "WHERE t.path = ? AND s.id != t.id ORDER BY s.date DESC, s.path, l.rowid", (path,))
        out: dict[str, dict] = {}
        for r in rows:
            item = out.setdefault(r["path"], {**{k: r[k] for k in ("path", "title", "client", "date")},
                                              "type": note_type(r["type"]), "contexts": []})
            if r["context"] and r["context"] not in item["contexts"]:
                item["contexts"].append(r["context"])
        return list(out.values())

    def graph(self, types=DEFAULT_GRAPH_TYPES, clients=None, missing: bool = False) -> dict:
        """Nodes (notes of the chosen types and clients) and the links between them, one edge per direction."""
        where, args = self._type_filter(types)
        if clients:
            where += f" AND n.client IN ({','.join('?' * len(clients))})"
            args += list(clients)
        nodes = self._rows(f"SELECT n.id, n.path, n.title, n.type, n.client, n.date FROM notes n WHERE {where}", args)
        ids = {n["id"]: n["path"] for n in nodes}
        edges, seen, ghosts = [], set(), {}
        with self._lock:
            links = self.db.execute("SELECT source, target, target_text FROM links").fetchall()
        for source, target, text in links:
            if source not in ids:
                continue
            if target is None:
                if not missing or is_attachment(text):
                    continue
                key = "missing:" + text.lower()
                ghosts.setdefault(key, text)
                pair = (ids[source], key)
            elif target in ids and target != source:
                pair = (ids[source], ids[target])
            else:
                continue
            if pair not in seen:
                seen.add(pair)
                edges.append({"source": pair[0], "target": pair[1]})
        degree: dict[str, int] = {}
        for e in edges:
            for end in (e["source"], e["target"]):
                degree[end] = degree.get(end, 0) + 1
        out = [{"id": n["path"], "title": n["title"], "type": note_type(n["type"]), "client": n["client"],
                "date": n["date"], "links": degree.get(n["path"], 0)} for n in nodes]
        out += [{"id": key, "title": posixpath.basename(text), "type": "missing", "client": None, "date": None,
                 "links": degree.get(key, 0)} for key, text in ghosts.items()]
        return {"nodes": out, "edges": edges}

    def client_stats(self) -> dict[str, dict]:
        """Per client folder: number of meetings, the newest meeting date, open items in the profile."""
        stats: dict[str, dict] = {}
        for r in self._rows("SELECT client, count(*) AS meetings, max(date) AS last FROM notes "
                            "WHERE type = 'call' AND client IS NOT NULL GROUP BY client"):
            stats[r["client"]] = {"meetings": r["meetings"], "last_meeting": r["last"], "open_items": 0, "profile": None}
        for r in self._rows("SELECT client, path, body FROM notes WHERE type = 'client' AND client IS NOT NULL"):
            if r["path"] != f"Clients/{r['client']}/{r['client']}.md":
                continue                                        # the profile is the note named like its folder
            item = stats.setdefault(r["client"], {"meetings": 0, "last_meeting": None, "open_items": 0})
            item["profile"] = r["path"]
            item["open_items"] = len(open_items(get_section(r["body"] or "", "Open Items") or ""))
        return stats

