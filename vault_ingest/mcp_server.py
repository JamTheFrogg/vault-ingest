"""The vault's MCP server for AI tools (Claude Desktop, Cursor, Codex), over stdio.

Each tool starts it as `python -m vault_ingest.mcp_server` with VAULT_INGEST_HOME set, so it works while the app is
closed. Tool names and arguments are the ones the owner's AI instructions already use (from lstpsche/obsidian-mcp),
for the subset kept here. Reads go through the note index; writes stay inside the vault, never delete, never touch
`.obsidian`, `_System/backups` or the inbox, and copy a note to `_System/backups` before changing it.
`python -m vault_ingest.mcp_server --check` opens everything once and exits: the app's connection test.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import functools
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import embeddings, models
from .config import home, load_settings
from .index import ATTACHMENT, NoteIndex, note_type
from .vault import (UnsafePathError, Vault, get_frontmatter, make_note, patch_section, remove_frontmatter_key,
                    set_frontmatter_value, split_frontmatter)

log = logging.getLogger("vault_ingest")
REFRESH_SECONDS = 3.0                  # the index is brought up to date before a read, at most this often
MAX_LIST = 5000
READ_MANY_FILES, READ_MANY_BYTES = (20, 100), (65536, 262144)     # (default, cap)
TAG = re.compile(r"(?<![\w&/#])#([\w][\w/-]*)")
KEY = re.compile(r"[A-Za-z0-9_][\w .-]{0,63}")


class ToolFailure(Exception):
    """A refusal the AI tool should read (bad path, missing note, ...)."""


class VaultTools:
    """The tools as plain methods, so they are tested without a transport."""

    def __init__(self, settings, data_home: Path | None = None, index: NoteIndex | None = None,
                 catalogue: dict | None = None):
        self.s = settings
        self.root = Path(settings.vault_path).resolve()
        self.home = Path(data_home or home())
        self.index = index or NoteIndex(self.root, self.home / "index.sqlite")
        self.cat = catalogue or models.load_catalogue()
        self.vault = Vault(self.root, (".",))
        self.inbox = settings.inbox_dir.strip("/")
        self.backups = f"{settings.system_dir.strip('/')}/backups"
        self._refreshed = 0.0

    # ---------------------------------------------------------------- paths
    def excluded(self, rel: str) -> bool:
        parts = rel.split("/")
        return any(p.startswith(".") for p in parts) or rel == self.inbox or rel.startswith(self.inbox + "/") \
            or rel == self.backups or rel.startswith(self.backups + "/")

    def _path(self, raw, *, note: bool = True) -> Path:
        """A vault path from a tool argument; refuses anything outside the vault or in a closed folder."""
        if not isinstance(raw, str) or "\x00" in raw:
            raise ToolFailure("The path must be text.")
        rel = raw.replace("\\", "/").strip()
        if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or rel.startswith("~"):
            raise ToolFailure("Paths are relative to the vault folder, for example Clients/Acme/Acme.md.")
        if note and rel and not rel.lower().endswith(".md"):
            if Path(rel).suffix.lower() in ATTACHMENT:
                raise ToolFailure("Only Markdown notes (.md) can be read or written with this tool.")
            rel += ".md"                                # "Clients/Acme/Acme" is the note Acme.md
        try:
            path = (self.root / rel).resolve()
        except (OSError, ValueError):
            raise ToolFailure("That path is not valid.") from None
        if path != self.root and self.root not in path.parents:
            raise ToolFailure("That path is outside the vault.")
        if path != self.root and self.excluded(path.relative_to(self.root).as_posix()):
            raise ToolFailure("That folder is closed to the vault tools (.obsidian, backups, the inbox).")
        if note and path == self.root:
            raise ToolFailure("Give the path of a note.")
        return path

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ToolFailure(f"There is no note {self.rel(path)}.") from None
        except (OSError, UnicodeDecodeError) as e:
            raise ToolFailure(f"{self.rel(path)} cannot be read: {e}") from None

    def fresh(self, force: bool = False) -> NoteIndex:
        if force or time.monotonic() - self._refreshed > REFRESH_SECONDS:
            self.index.refresh()
            self._refreshed = time.monotonic()
        return self.index

    # ---------------------------------------------------------------- writing
    def _backup(self, path: Path) -> None:
        """A copy in _System/backups before a note changes, named as the pipeline names its backups."""
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = self.root / self.backups
        dest, n = folder / f"{path.stem} {stamp}{path.suffix}", 2
        while dest.exists():
            dest, n = folder / f"{path.stem} {stamp} ({n}){path.suffix}", n + 1
        self.vault.check(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    def _write(self, path: Path, text: str, *, backup: bool) -> None:
        try:
            if backup:
                self._backup(path)
            self.vault.write(path, text)
        except UnsafePathError as e:
            raise ToolFailure(str(e)) from None
        self.fresh(force=True)                      # a search right after the write finds it

    # ---------------------------------------------------------------- navigation
    def vault_list(self, path: str = "", recursive: bool = False, glob: str | None = None, format: str = "list",
                   max_depth: int | None = None, include_metadata: bool = False) -> str:
        base = self._path(path, note=False) if path else self.root
        if not base.is_dir():
            raise ToolFailure(f"There is no folder {path}.")
        tree = format == "tree"
        if format not in ("list", "tree"):
            raise ToolFailure('format is "list" or "tree".')
        rows: list[tuple[int, str, bool]] = []

        def walk(folder: Path, depth: int) -> None:
            for entry in sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                rel = self.rel(entry)
                if self.excluded(rel) or len(rows) >= MAX_LIST:
                    continue
                is_dir = entry.is_dir()
                matched = not glob or fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(entry.name, glob)
                if (is_dir and (tree or not glob)) or (not is_dir and matched):   # a glob picks files
                    rows.append((depth, rel, is_dir))
                if is_dir and (recursive or tree) and (max_depth is None or depth + 1 < max_depth):
                    walk(entry, depth + 1)

        walk(base, 0)
        if tree:
            return "\n".join(("  " * d) + Path(r).name + ("/" if is_dir else "") for d, r, is_dir in rows)
        if not include_metadata:
            return json.dumps([r + ("/" if is_dir else "") for _d, r, is_dir in rows], ensure_ascii=False)
        index = self.fresh()
        out = []
        for _d, r, is_dir in rows:
            row = None if is_dir else index.note(r)
            out.append({"path": r + "/"} if is_dir else {"path": r} if row is None else
                       {"path": r, "title": row["title"], "type": note_type(row["type"]), "client": row["client"],
                        "date": row["date"], "size": (self.root / r).stat().st_size})
        return json.dumps(out, ensure_ascii=False)

    # ---------------------------------------------------------------- reading
    def note_read(self, path: str, max_bytes: int | None = None) -> str:
        text = self._read(self._path(path))
        if max_bytes and len(text.encode("utf-8")) > max_bytes:
            cut = text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
            return cut + f"\n\n[cut at {max_bytes} of {len(text.encode('utf-8'))} bytes]"
        return text

    def note_read_many(self, paths: list[str] | None = None, dir: str | None = None, recursive: bool = False,
                       glob: str | None = None, max_files: int | None = None, max_bytes: int | None = None) -> str:
        if (paths is None) == (dir is None):
            raise ToolFailure("Give exactly one of paths or dir.")
        max_files = min(max_files or READ_MANY_FILES[0], READ_MANY_FILES[1])
        max_bytes = min(max_bytes or READ_MANY_BYTES[0], READ_MANY_BYTES[1])
        if paths is not None:
            wanted = list(paths)
        else:
            listed = json.loads(self.vault_list(dir, recursive=recursive, glob=glob))
            wanted = [p for p in listed if p.lower().endswith(".md")]
        notes, skipped, used = [], [], 0
        for n, raw in enumerate(wanted):
            if n >= max_files:
                skipped.append({"path": raw, "reason": "file_limit"})
                continue
            try:
                path = self._path(raw)
                text = path.read_text(encoding="utf-8")
            except ToolFailure as e:
                skipped.append({"path": raw, "reason": "refused", "message": str(e)})
                continue
            except FileNotFoundError:
                skipped.append({"path": raw, "reason": "not_found"})
                continue
            except (OSError, UnicodeDecodeError):
                skipped.append({"path": raw, "reason": "unreadable"})
                continue
            size = len(text.encode("utf-8"))
            if used + size > max_bytes:
                skipped.append({"path": raw, "reason": "byte_limit"})
                continue
            used += size
            notes.append({"path": self.rel(path), "content": text})
        return json.dumps({"notes": notes, "skipped": skipped[:100], "skipped_count": len(skipped),
                           "content_bytes": used}, ensure_ascii=False)

    # ---------------------------------------------------------------- search
    @staticmethod
    def _hit(r: dict) -> dict:
        return {**{k: r[k] for k in ("path", "title", "type", "client", "date")},
                "snippet": "".join(f"**{t}**" if hit else t for t, hit in r["snippet"])}

    def search_text(self, query: str, max_results: int | None = None, context_length: int | None = None,
                    fuzzy: bool | None = None, fields: list[str] | None = None) -> str:
        limit = max(1, min(int(max_results or 20), 200))
        index = self.fresh()
        rows = index.search(query, limit) or index.search(query, limit, any_term=True)   # every word, else any
        return json.dumps([self._hit(r) for r in rows], ensure_ascii=False)

    def search_semantic(self, query: str, top_k: int | None = None, include_content: bool = False,
                        lexical_prefetch: bool = False, alpha: float | None = None) -> str:
        limit = max(1, min(int(top_k or 10), 100))
        index = self.fresh()
        model = embeddings.model_id(self.home, self.cat)
        client = embeddings.running_client(self.home, self.cat) if model else None
        message = None
        rows: list[dict] | None = None
        if client is not None:
            try:
                vector = client.embed([query])[0]
                rows = index.semantic(vector, model, limit, query=query, lexical=bool(lexical_prefetch),
                                      alpha=0.25 if alpha is None else alpha)
                if not rows and not index.embedded_count(model)["done"]:
                    rows, message = None, "No note has been prepared for search by meaning yet."
            except (embeddings.EmbeddingUnavailable, ValueError) as e:
                message = str(e)
        else:
            message = ("Search by meaning is off or not downloaded (vault-ingest > Settings > AI engine)." if not model
                       else "Search by meaning needs the vault-ingest app running.")
        if rows is None:
            hits = index.search(query, limit) or index.search(query, limit, any_term=True)
            return json.dumps({"mode": "keyword", "message": message + " These are keyword results instead.",
                               "results": [self._hit(r) for r in hits]}, ensure_ascii=False)
        out = []
        for r in rows:
            item = {k: r[k] for k in ("path", "title", "type", "client", "date", "score", "cosine", "heading", "passage")}
            if include_content:
                try:
                    item["content"] = (self.root / r["path"]).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    item["content"] = None
            out.append(item)
        return json.dumps({"mode": "hybrid" if lexical_prefetch else "semantic", "results": out}, ensure_ascii=False)

    def _notes(self) -> list[tuple[str, str]]:
        """(path, text) of every indexed note. shortcut: reads every file per call, fine for a few thousand notes."""
        out = []
        for r in self.fresh()._rows("SELECT path FROM notes ORDER BY path"):
            try:
                out.append((r["path"], (self.root / r["path"]).read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
        return out

    def search_metadata(self, type: str, tag: str | None = None, include_nested: bool | None = None,
                        field: str | None = None, value: Any = None, operator: str | None = None) -> str:
        found = []
        if type == "tag":
            if not tag:
                raise ToolFailure("Give a tag.")
            want = tag.lstrip("#").lower()
            nested = include_nested is not False
            for path, text in self._notes():
                fm = get_frontmatter(text)
                raw = fm.get("tags") or []
                tags = {str(t).lstrip("#").lower() for t in ([raw] if isinstance(raw, str) else raw if isinstance(raw, list) else [])}
                tags |= {t.lower() for t in TAG.findall(split_frontmatter(text)[1])}
                if want in tags or nested and any(t.startswith(want + "/") for t in tags):
                    found.append({"path": path, "tags": sorted(tags)})
        elif type == "frontmatter":
            op = operator or "eq"
            if not field or op not in ("eq", "contains", "exists"):
                raise ToolFailure('Give field, and operator eq, contains or exists.')
            for path, text in self._notes():
                fm = get_frontmatter(text)
                if field not in fm:
                    continue
                have = fm[field]
                if op == "exists" or op == "eq" and (have == value or str(have) == str(value)) or op == "contains" and (
                        value in have if isinstance(have, list) else str(value).lower() in str(have).lower()):
                    found.append({"path": path, "value": have})
        else:
            raise ToolFailure('type is "tag" or "frontmatter".')
        return json.dumps(found, ensure_ascii=False, default=str)

    def wikilinks(self, query: str, path: str | None = None) -> str:
        index = self.fresh()
        rel = self.rel(self._path(path)) if path else None
        if query in ("backlinks", "outgoing") and not rel:
            raise ToolFailure(f"{query} needs a path.")
        if query == "backlinks":
            out = [{"source_path": b["path"], "title": b["title"], "contexts": b["contexts"]} for b in index.backlinks(rel)]
        elif query == "outgoing":
            out = [{"target": x["target"], "path": x["path"], "heading": x["heading"], "embed": x["embed"]}
                   for x in index.links(self._read(self.root / rel), rel)]
        elif query == "broken":
            out = index.broken(rel)
        elif query == "orphans":
            out = index.orphans()
        else:
            raise ToolFailure('query is "backlinks", "outgoing", "broken" or "orphans".')
        return json.dumps(out, ensure_ascii=False)

    # ---------------------------------------------------------------- writing
    def note_create(self, path: str, content: str | None = None, frontmatter: dict | None = None) -> str:
        target = self._path(path)
        if target.exists():
            raise ToolFailure(f"{self.rel(target)} already exists. Use note_patch or note_write to change it.")
        if frontmatter is not None and not isinstance(frontmatter, dict):
            raise ToolFailure("frontmatter must be an object.")
        text = make_note(frontmatter, content or "") if frontmatter else (content or "")
        self._write(target, text, backup=False)
        return f"Created {self.rel(target)}"

    def note_write(self, path: str, content: str) -> str:
        target = self._path(path)
        self._read(target)                          # must exist: a new note is note_create
        self._write(target, content, backup=True)
        return f"Wrote {self.rel(target)} (the old version is in {self.backups})"

    def note_patch(self, path: str, operation: str, target_type: str, target: str, content: str) -> str:
        if operation not in ("append", "prepend", "replace"):
            raise ToolFailure('operation is "append", "prepend" or "replace".')
        note = self._path(path)
        text = self._read(note)
        if target_type == "heading":
            new = patch_section(text, target, operation, content)
            if new is None:
                heads = re.findall(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", split_frontmatter(text)[1], re.M)
                raise ToolFailure(f"No heading {target!r} in {self.rel(note)}. Headings: " + ", ".join(heads[:40]))
        elif target_type == "block":
            new = self._patch_block(text, target, operation, content)
        elif target_type == "frontmatter":
            fm = get_frontmatter(text)
            if operation == "replace":
                return self._set_field(note, text, target, content)
            old = fm.get(target)
            items = [] if old is None else old if isinstance(old, list) else [old]
            return self._set_field(note, text, target, [content, *items] if operation == "prepend" else [*items, content])
        else:
            raise ToolFailure('target_type is "heading", "block" or "frontmatter".')
        self._write(note, new, backup=True)
        return f"Patched {self.rel(note)} ({operation} at {target_type} {target!r})"

    @staticmethod
    def _patch_block(text: str, block: str, operation: str, content: str) -> str:
        ref = block.lstrip("^")
        m = re.search(rf"^(.*?)[ \t]+\^{re.escape(ref)}[ \t]*$", text, re.M)
        if not m:
            raise ToolFailure(f"No block ^{ref} in the note.")
        add = content.strip("\n")
        line = {"append": f"{m.group(0)}\n{add}", "prepend": f"{add}\n{m.group(0)}",
                "replace": f"{add} ^{ref}"}[operation]
        return text[:m.start()] + line + text[m.end():]

    def _set_field(self, note: Path, text: str, key: str, value) -> str:
        if not KEY.fullmatch(key or ""):
            raise ToolFailure("That is not a frontmatter key.")
        new = set_frontmatter_value(text, key, value)
        fm = get_frontmatter(new)
        if fm.get(key) != value and str(fm.get(key)) != str(value):
            raise ToolFailure("That value cannot be written into this note's frontmatter. Nothing was changed.")
        self._write(note, new, backup=True)
        return f"Set {key} in {self.rel(note)}"

    def frontmatter(self, action: str, path: str, key: str | None = None, value: Any = None) -> str:
        note = self._path(path)
        text = self._read(note)
        if action == "get":
            fm = get_frontmatter(text)
            return json.dumps(fm if split_frontmatter(text)[0] else None, ensure_ascii=False, default=str)
        if not key:
            raise ToolFailure(f"{action} needs a key.")
        if action == "set":
            return self._set_field(note, text, key, value)
        if action == "remove":
            if key not in get_frontmatter(text):
                return f"{key} is not set in {self.rel(note)}"
            new = remove_frontmatter_key(text, key)
            if key in get_frontmatter(new):
                raise ToolFailure("That key cannot be removed safely. Nothing was changed.")
            self._write(note, new, backup=True)
            return f"Removed {key} from {self.rel(note)}"
        raise ToolFailure('action is "get", "set" or "remove".')


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITES = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
CHANGES = {**WRITES, "destructiveHint": True}
DESCRIPTIONS = {
    "vault_list": ("List files and folders of the vault. path: a folder (default the vault root); recursive; glob; "
                   'format "list" (JSON array) or "tree"; max_depth; include_metadata (title, type, client, date).', READ_ONLY),
    "note_read": ("Read a note's full Markdown (frontmatter included). Paths are relative to the vault root.", READ_ONLY),
    "note_read_many": ("Read several notes at once: exactly one of paths or dir (recursive, glob). At most 100 files "
                       "and 262144 bytes; what did not fit is listed in skipped.", READ_ONLY),
    "search_text": ("Keyword search over note titles, aliases and text, best first, with snippets. Case and "
                    "diacritics are ignored. Notes with every word come first; else notes with any word. "
                    "fuzzy and fields are accepted and ignored.", READ_ONLY),
    "search_semantic": ("Search by meaning: notes about the query even without the same words, each with its "
                        "closest passage. lexical_prefetch: true ranks only keyword candidates, blending "
                        "alpha * keyword + (1 - alpha) * meaning (alpha default 0.25). include_content adds the "
                        "full note. Without the app's embedding server it answers with keyword results and says so.",
                        READ_ONLY),
    "search_metadata": ('Find notes by tag (type "tag", tag, include_nested) or by a frontmatter field '
                        '(type "frontmatter", field, operator eq|contains|exists, value).', READ_ONLY),
    "wikilinks": ('Links: query "backlinks" or "outgoing" (with path), "broken" (optional path) or "orphans".',
                  READ_ONLY),
    "note_create": ("Create a new note (content, optional frontmatter object). Folders are created. Refuses to "
                    "overwrite an existing note.", WRITES),
    "note_write": ("Replace a whole existing note. The old version is kept in _System/backups.", CHANGES),
    "note_patch": ('Change one part of a note: operation "append", "prepend" or "replace"; target_type "heading" '
                   '(target "Work Log" or "## Work Log"), "block" (a ^block-id) or "frontmatter" (a field). '
                   "The old version is kept in _System/backups.", CHANGES),
    "frontmatter": ('Frontmatter of a note: action "get", "set" (key, value) or "remove" (key). Changes keep the old '
                    "version in _System/backups.", CHANGES),
}


def build(tools: VaultTools):
    """The MCP server with every tool of `tools` registered under its name."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_types import ToolAnnotations

    server = MCPServer("vault", instructions="The user's Markdown vault (an Obsidian vault): meeting notes, client "
                                             "profiles, transcripts. Paths are relative to the vault root.")
    for name, (text, hints) in DESCRIPTIONS.items():
        method = getattr(tools, name)

        @functools.wraps(method)
        def call(*args, _method=method, **kw):
            try:
                return _method(*args, **kw)
            except ToolFailure as e:
                raise ToolError(str(e)) from None

        server.add_tool(call, name=name, description=text, annotations=ToolAnnotations(**hints),
                        structured_output=False)
    return server


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)     # stdout carries the protocol only
    settings = load_settings(home() / ".env")
    tools = VaultTools(settings)
    if "--check" in argv:
        changed = tools.fresh(force=True)._rows("SELECT count(*) AS n FROM notes")[0]["n"]
        print(f"ok: {changed} notes in {tools.root}")
        return 0
    build(tools).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
