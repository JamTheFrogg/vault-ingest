"""Vault file operations: safe paths, atomic writes, frontmatter and markdown section editing."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import yaml

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f#^\[\]]')


class UnsafePathError(RuntimeError):
    pass


def sanitize_name(name: str, max_len: int = 90) -> str:
    """Make a string safe as a Windows/macOS/Linux file name and an Obsidian link target."""
    name = INVALID_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_len].rstrip(" .") or "Untitled"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def word_count(text: str) -> int:
    return len(text.split())


class Vault:
    """All writes go through this class, which refuses paths outside the allowed folders."""

    def __init__(self, root: Path, allowed: tuple[str, ...]):
        self.root = root.resolve()
        self.allowed = tuple((self.root / a).resolve() for a in allowed)

    # ---------------------------------------------------------------- paths
    def check(self, path: Path) -> Path:
        p = Path(path)
        p = (self.root / p).resolve() if not p.is_absolute() else p.resolve()
        if not any(p == a or a in p.parents for a in self.allowed):
            raise UnsafePathError(f"Refusing to write outside allowed vault folders: {p}")
        return p

    def rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root).as_posix()

    def link(self, path: Path, alias: str | None = None) -> str:
        """Obsidian wikilink using the full vault path (unambiguous)."""
        target = self.rel(path)
        if target.endswith(".md"):
            target = target[:-3]
        return f"[[{target}|{alias}]]" if alias else f"[[{target}]]"

    # ---------------------------------------------------------------- writes
    def write(self, path: Path, content: str) -> Path:
        p = self.check(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=p.suffix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return p

    def move(self, src: Path, dest: Path) -> Path:
        """Move a file, never overwriting: adds ' (2)', ' (3)'... if the name is taken."""
        self.check(src)
        dest = self.check(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        final = dest
        n = 2
        while final.exists():
            final = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
            n += 1
        shutil.move(str(src), str(final))
        return final

    def copy(self, src: Path, dest: Path) -> Path:
        """Copy a file (the source may be outside the vault). Reuses `dest` if it already holds the same file
        (same size and modified time); a different file there gets ' (2)', ' (3)'... instead of being overwritten."""
        src = Path(src)
        dest = self.check(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        st = src.stat()
        final = dest
        n = 2
        while final.exists():
            fs = final.stat()
            if fs.st_size == st.st_size and int(fs.st_mtime) == int(st.st_mtime):
                return final
            final = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
            n += 1
        shutil.copy2(str(src), str(final))
        return final

    def mkdir(self, path: Path) -> Path:
        p = self.check(path)
        p.mkdir(parents=True, exist_ok=True)
        return p


# -------------------------------------------------------------------- frontmatter
FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def make_note(frontmatter: dict, body: str) -> str:
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=1000).strip()
    return f"---\n{fm}\n---\n{body.rstrip()}\n"


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block_including_dashes, body)."""
    m = FM_RE.match(text)
    if not m:
        return "", text
    return text[:m.end()], text[m.end():]


def set_frontmatter_field(text: str, key: str, value: str) -> str:
    """Set one scalar field without re-serializing (keeps the user's own YAML formatting)."""
    fm, body = split_frontmatter(text)
    line = f"{key}: {value}"
    if not fm:
        return f"---\n{line}\n---\n{body}"
    pattern = re.compile(rf"^{re.escape(key)}\s*:.*$", re.M)
    if pattern.search(fm):
        fm = pattern.sub(line, fm, count=1)
    else:
        fm = re.sub(r"\n---[ \t]*(\n|\Z)", lambda m: f"\n{line}\n---{m.group(1)}", fm, count=1)
    return fm + body


def get_frontmatter(text: str) -> dict:
    m = FM_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


# -------------------------------------------------------------------- sections
def _section_bounds(text: str, heading: str) -> tuple[int, int, int] | None:
    """Return (heading_start, body_start, body_end) for '## heading', or None."""
    m = re.search(rf"^##[ \t]+{re.escape(heading)}[ \t]*$", text, re.M | re.I)
    if not m:
        return None
    nxt = re.compile(r"^#{1,2}\s+\S", re.M).search(text, m.end())
    body_end = nxt.start() if nxt else len(text)
    return m.start(), m.end(), body_end


def get_section(text: str, heading: str) -> str | None:
    b = _section_bounds(text, heading)
    return text[b[1]:b[2]].strip("\n") if b else None


def retract_lines(text: str, lines: list[str], keep_section: str = "Snapshot") -> str:
    """Remove exact lines, except inside `keep_section` (hand edits there stay)."""
    drop = {line.strip() for line in lines if line and line.strip()}
    if not drop:
        return text
    bounds = _section_bounds(text, keep_section)
    out: list[str] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        start = offset
        offset += len(line)
        in_keep = bounds is not None and bounds[1] <= start < bounds[2]
        if line.strip() in drop and not in_keep:
            continue
        out.append(line)
    return "".join(out)


def replace_section(text: str, heading: str, new_body: str) -> str:
    b = _section_bounds(text, heading)
    block = f"\n{new_body.strip()}\n\n"
    if not b:
        return text.rstrip() + f"\n\n## {heading}{block}"
    return text[:b[1]] + block + text[b[2]:].lstrip("\n")


def append_to_section(text: str, heading: str, lines: list[str]) -> str:
    if not lines:
        return text
    b = _section_bounds(text, heading)
    if not b:
        return text.rstrip() + f"\n\n## {heading}\n" + "\n".join(lines) + "\n"
    existing = text[b[1]:b[2]].rstrip()
    # drop template placeholder lines like "- YYYY-MM-DD — decision (link to call)"
    kept = [l for l in existing.split("\n") if not _is_placeholder(l)]
    existing = "\n".join(kept).rstrip()
    body = (existing + "\n" if existing.strip() else "\n") + "\n".join(lines)
    return text[:b[1]] + body + "\n\n" + text[b[2]:].lstrip("\n")


def _is_placeholder(line: str) -> bool:
    l = line.strip()
    return bool(re.match(r"^- (\[ \] )?(YYYY-MM-DD|item —)", l))


def heading_bounds(text: str, heading: str) -> tuple[int, int, int] | None:
    """(heading_start, body_start, body_end) for a heading of any level, given as "Log" or "## Log". The body ends
    at the next heading of any level, so a patch never moves or replaces a sub-section."""
    target = re.sub(r"^#{1,6}\s+", "", heading.strip()).strip()
    if not target:
        return None
    m = re.search(rf"^(#{{1,6}})[ \t]+{re.escape(target)}[ \t]*#*[ \t]*$", text, re.M | re.I)
    if not m:
        return None
    body_start = m.end() + 1 if m.end() < len(text) else m.end()
    nxt = re.compile(r"^#{1,6}[ \t]+\S", re.M).search(text, body_start)
    return m.start(), body_start, nxt.start() if nxt else len(text)


def patch_section(text: str, heading: str, operation: str, content: str) -> str | None:
    """`append` / `prepend` `content` inside the section under `heading`, or `replace` its body; None when the
    heading is not there."""
    b = heading_bounds(text, heading)
    if not b:
        return None
    head, body, after = text[:b[1]], text[b[1]:b[2]], text[b[2]:]
    if not head.endswith("\n"):
        head += "\n"
    add = content.strip("\n")
    if operation == "prepend":
        body = add + "\n" + (body.lstrip("\n") if body.strip() else "")
    elif operation == "append":
        body = (body.rstrip("\n") + "\n" if body.strip() else "") + add + "\n"
    else:
        body = add + "\n"
    return head + body.rstrip("\n") + "\n" + ("\n" + after.lstrip("\n") if after else "")


def set_frontmatter_value(text: str, key: str, value) -> str:
    """Set `key` to any YAML value (written on one line), keeping the rest of the frontmatter as written."""
    line = f"{key}: " + yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10**6) \
        .strip().removesuffix("...").strip()
    fm, body = split_frontmatter(text)
    if not fm:
        return f"---\n{line}\n---\n{body}"
    lines, out, i, found = fm.split("\n"), [], 0, False
    while i < len(lines):
        if not found and re.match(rf"^{re.escape(key)}\s*:", lines[i]):
            found = True
            out.append(line)
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t", "- ")) or lines[i] == "-"):
                i += 1                                          # the old value's block lines
            continue
        out.append(lines[i])
        i += 1
    if not found:
        close = max(j for j, x in enumerate(out) if x.strip() == "---")
        out.insert(close, line)
    return "\n".join(out) + body


def remove_frontmatter_key(text: str, key: str) -> str:
    fm, body = split_frontmatter(text)
    lines, out, i = fm.split("\n"), [], 0
    while i < len(lines):
        if re.match(rf"^{re.escape(key)}\s*:", lines[i]):
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t", "- ")) or lines[i] == "-"):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if all(x.strip() in ("---", "") for x in out):
        return body                                             # the last key: no empty block left behind
    return "\n".join(out) + body


def list_entries(section_body: str) -> list[str]:
    return [l for l in section_body.split("\n") if re.match(r"^\s*- ", l) and not _is_placeholder(l)]


# -------------------------------------------------------------------- open items
def normalize(s: str) -> str:
    s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"^\s*- \[[ xX]\]\s*", "", s)
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def open_items(section_body: str) -> list[str]:
    """Text of unchecked task lines (without the '- [ ] ' prefix)."""
    out = []
    for l in section_body.split("\n"):
        m = re.match(r"^\s*- \[ \]\s+(.*)$", l)
        if m and not _is_placeholder(l):
            out.append(m.group(1).strip())
    return out


def merge_open_items(section_body: str, resolved: list[str], new_lines: list[str], today: str) -> str:
    """Check off resolved items (fuzzy match), append new ones (deduped). Never removes lines."""
    lines = [l for l in section_body.split("\n") if not _is_placeholder(l)]
    resolved_norm = [normalize(r) for r in resolved if normalize(r)]
    for i, l in enumerate(lines):
        m = re.match(r"^(\s*)- \[ \]\s+(.*)$", l)
        if not m:
            continue
        n = normalize(m.group(2))
        if any(r == n or (len(r) > 12 and (r in n or n in r)) for r in resolved_norm):
            lines[i] = f"{m.group(1)}- [x] {m.group(2)} ✅ {today}"
    existing_norm = {normalize(l) for l in lines if l.strip()}
    for nl in new_lines:
        if normalize(nl) not in existing_norm:
            lines.append(f"- [ ] {nl}")
            existing_norm.add(normalize(nl))
    return "\n".join(l for l in lines if l is not None).strip("\n")


# -------------------------------------------------------------------- registry
class Registry:
    """_System/ingest-log.json : file hash -> what was produced."""

    def __init__(self, vault: Vault, path: Path):
        self.vault = vault
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                backup = path.with_suffix(f".corrupt-{dt.datetime.now():%Y%m%d%H%M%S}.json")
                shutil.copy2(path, backup)
                self.data = {}

    def get(self, digest: str) -> dict | None:
        return self.data.get(digest)

    def put(self, digest: str, record: dict) -> None:
        self.data[digest] = record
        self.vault.write(self.path, json.dumps(self.data, indent=2, ensure_ascii=False))

    def remove(self, digest: str) -> None:
        if digest in self.data:
            del self.data[digest]
            self.vault.write(self.path, json.dumps(self.data, indent=2, ensure_ascii=False))

    def find_by_source(self, rel_path: str) -> tuple[str, dict] | None:
        for k, v in self.data.items():
            if v.get("raw") == rel_path or v.get("source") == rel_path:
                return k, v
        return None
