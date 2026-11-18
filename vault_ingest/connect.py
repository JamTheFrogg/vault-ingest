"""Settings > Connect AI tools: add or remove this vault's MCP server in Claude Desktop, Cursor and Codex.

Each tool keeps its servers in one file of its own. A change here: reads and parses that file (and refuses a file it
cannot parse), copies it to `<file>.bak-<time>`, changes only the `vault` entry, checks that every other entry is
still exactly as it was, then replaces the file in one step. JSON is parsed and written back whole; TOML (Codex) is
edited as text, because the standard library cannot write TOML, and the result is parsed again before it is saved.
An old `obsidian` entry is only reported; `remove_old` takes it out when the user clicks for it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Callable

from .config import PROJECT_DIR, home

SERVER = "vault"
TOOLS = {"claude": "Claude Desktop", "cursor": "Cursor", "codex": "Codex"}
RESTART = {
    "claude": "Quit Claude Desktop completely (also from the tray) and open it again.",
    "cursor": "Cursor loads it by itself. If the tools do not show, reload the window.",
    "codex": "Start a new Codex session.",
}
INSTRUCTION = PROJECT_DIR / "configs" / "standing-instruction.txt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ConnectError(RuntimeError):
    pass


def this_os() -> str:
    return {"darwin": "macos", "windows": "windows"}.get(platform.system().lower(), "linux")


def config_path(tool: str, *, os_name: str | None = None, env=None, home_dir: Path | None = None) -> Path:
    """Where `tool` keeps its MCP servers on this computer."""
    env = os.environ if env is None else env
    os_name = os_name or this_os()
    user = Path(home_dir or Path.home())
    if tool == "claude":
        name = "claude_desktop_config.json"
        if os_name == "windows":
            regular = Path(env.get("APPDATA") or user / "AppData" / "Roaming") / "Claude"
            if not regular.is_dir():
                # unverified: a Microsoft Store (MSIX) install keeps AppData inside its package folder
                local = Path(env.get("LOCALAPPDATA") or user / "AppData" / "Local")
                store = sorted(local.glob("Packages/Claude_*/LocalCache/Roaming/Claude"))
                if store:
                    return store[0] / name
            return regular / name
        if os_name == "macos":
            return user / "Library" / "Application Support" / "Claude" / name
        return Path(env.get("XDG_CONFIG_HOME") or user / ".config") / "Claude" / name   # unverified: no Linux build
    if tool == "cursor":
        return user / ".cursor" / "mcp.json"
    if tool == "codex":
        return Path(env.get("CODEX_HOME") or user / ".codex") / "config.toml"
    raise KeyError(tool)


def server_entry(python: str | None = None, data_home: Path | None = None) -> dict:
    """The command every tool starts: this engine's own interpreter (with -I when it is the bundled one)."""
    args = (["-I"] if python is None and sys.flags.isolated else []) + ["-m", "vault_ingest.mcp_server"]
    return {"command": python or sys.executable, "args": args, "env": {"VAULT_INGEST_HOME": str(data_home or home())}}


# ------------------------------------------------------------------ reading
def _servers(tool: str, text: str) -> dict:
    """The servers in a config file's text. Raises ConnectError for a file the tool itself could not read."""
    if not text.strip():
        return {}
    try:
        data = tomllib.loads(text) if tool == "codex" else json.loads(text)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        raise ConnectError(f"The file cannot be read: {e}") from None
    if not isinstance(data, dict):
        raise ConnectError("The file does not hold settings.")
    servers = data.get("mcp_servers" if tool == "codex" else "mcpServers", {})
    if not isinstance(servers, dict):
        raise ConnectError("Its server list is not in the expected form.")
    return servers


def _is_old(name: str, value) -> bool:
    """The server the old setup used (lstpsche/obsidian-mcp), by name or by its program."""
    return name != SERVER and ("obsidian" in name.lower() or "obsidian-mcp" in json.dumps(value).lower())


def status(tool: str, want: dict, *, path: Path | None = None, appimage: bool | None = None) -> dict:
    """One row of the screen: not_installed, not_connected, connected, changed (ours points elsewhere) or error."""
    path = path or config_path(tool)
    row = {"id": tool, "name": TOOLS[tool], "path": str(path), "state": "not_connected", "reason": None,
           "old": [], "restart": RESTART[tool], "snippet": snippet(tool, want), "connectable": True}
    if appimage if appimage is not None else bool(os.getenv("APPIMAGE")):
        row["connectable"] = False
        row["reason"] = "The AppImage starts from a new folder each time. Install the .deb, or copy the config."
    if not path.parent.is_dir() and not path.exists():
        return {**row, "state": "not_installed"}
    try:
        servers = _servers(tool, path.read_text(encoding="utf-8") if path.exists() else "")
    except (OSError, UnicodeDecodeError, ConnectError) as e:
        return {**row, "state": "error", "reason": str(e)}
    row["old"] = [name for name, value in servers.items() if _is_old(name, value)]
    mine = servers.get(SERVER)
    if mine is None:
        return row
    if _same(mine, want):
        return {**row, "state": "connected"}
    command = mine.get("command") if isinstance(mine, dict) else None
    return {**row, "state": "changed", "reason": f"It starts {command or 'something else'}. Connect again to fix it."}


def _same(entry, want: dict) -> bool:
    return isinstance(entry, dict) and entry.get("command") == want["command"] and \
        list(entry.get("args") or []) == want["args"] and (entry.get("env") or {}) == want["env"]


# ------------------------------------------------------------------ writing
def connect(tool: str, want: dict, *, path: Path | None = None) -> dict:
    return _change(tool, path or config_path(tool), set_to=want)


def disconnect(tool: str, *, path: Path | None = None) -> dict:
    return _change(tool, path or config_path(tool), remove=SERVER)


def remove_old(tool: str, name: str, *, path: Path | None = None) -> dict:
    path = path or config_path(tool)
    servers = _servers(tool, path.read_text(encoding="utf-8") if path.exists() else "")
    if name not in servers or not _is_old(name, servers[name]):
        raise ConnectError(f"There is no old entry {name!r} in {path}.")
    return _change(tool, path, remove=name)


def _change(tool: str, path: Path, *, set_to: dict | None = None, remove: str | None = None) -> dict:
    """Write the file with one entry set or removed. Returns {path, backup}."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    before = _servers(tool, text)
    name = remove or SERVER
    if remove is not None and name not in before:
        return {"path": str(path), "backup": None}
    new = (_toml_set if tool == "codex" else _json_set)(text, name, set_to)
    after = _servers(tool, new)
    others = {k: v for k, v in before.items() if k != name}
    if {k: v for k, v in after.items() if k != name} != others or (set_to is None) != (name not in after) \
            or (set_to is not None and not _same(after[name], set_to)):
        raise ConnectError(f"{path.name} is written in a form this app does not change. Edit it by hand, "
                           "or use Copy the config.")
    if tool != "codex":
        # the rest of the file (Claude Desktop's own settings) must survive too
        rest = lambda t: {k: v for k, v in (json.loads(t) if t.strip() else {}).items() if k != "mcpServers"}
        if rest(new) != rest(text):
            raise ConnectError(f"{path.name} would lose other settings. Nothing was changed.")
    backup = _backup(path) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"path": str(path), "backup": str(backup) if backup else None}


def _backup(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target, n = path.with_name(f"{path.name}.bak-{stamp}"), 2
    while target.exists():
        target, n = path.with_name(f"{path.name}.bak-{stamp}-{n}"), n + 1
    target.write_bytes(path.read_bytes())
    return target


def _json_set(text: str, name: str, value: dict | None) -> str:
    data = json.loads(text) if text.strip() else {}
    servers = data.setdefault("mcpServers", {})
    if value is None:
        servers.pop(name, None)
    else:
        servers[name] = value
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _toml_str(value: str) -> str:
    # a literal string keeps Windows back-slashes as they are; a basic string when it holds a quote
    return f"'{value}'" if "'" not in value and "\n" not in value else json.dumps(value)


def toml_block(name: str, value: dict) -> str:
    lines = [f"[mcp_servers.{name}]", f"command = {_toml_str(value['command'])}",
             f"args = [{', '.join(_toml_str(a) for a in value['args'])}]"]
    if value.get("env"):
        lines += ["", f"[mcp_servers.{name}.env]"] + [f"{k} = {_toml_str(v)}" for k, v in value["env"].items()]
    return "\n".join(lines) + "\n"


def _toml_set(text: str, name: str, value: dict | None) -> str:
    """Remove the tables of server `name` ([mcp_servers.name] and its sub-tables), then append the new ones."""
    header = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")
    ours = re.compile(rf"""^mcp_servers\s*\.\s*(?:{re.escape(name)}|"{re.escape(name)}"|'{re.escape(name)}')\s*(?:\..*)?$""")
    kept, skipping = [], False
    for line in text.splitlines(keepends=True):
        m = header.match(line)
        if m:
            skipping = bool(ours.match(m.group(1)))
        if not skipping:
            kept.append(line)
    out = "".join(kept).rstrip("\n")
    if value is not None:
        out = (out + "\n\n" if out else "") + toml_block(name, value)
    return out + ("\n" if out and not out.endswith("\n") else "")


# ------------------------------------------------------------------ what the screen shows
def snippet(tool: str, want: dict) -> str:
    """The config to paste by hand."""
    if tool == "codex":
        return toml_block(SERVER, want)
    return json.dumps({"mcpServers": {SERVER: want}}, indent=2, ensure_ascii=False) + "\n"


def instruction() -> str:
    """The standing instruction for the tools' instruction fields, with this server's name."""
    try:
        return INSTRUCTION.read_text(encoding="utf-8").replace('"obsidian" MCP tools', f'"{SERVER}" MCP tools').strip()
    except OSError:
        return ""


def check_server(want: dict, run: Callable = subprocess.run) -> str | None:
    """Start the server once the way the tools will (`--check`): None when it works, else what went wrong."""
    try:
        proc = run([want["command"], *want["args"], "--check"], env={**os.environ, **want["env"]},
                   capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"The vault server did not start: {e}"
    if proc.returncode == 0:
        return None
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return f"The vault server did not start: {tail[-1] if tail else f'exit code {proc.returncode}'}"


def overview(want: dict | None = None, *, paths: dict[str, Path] | None = None, check: str | None = "unknown") -> dict:
    """Every tool's row, the server check and the instruction text."""
    want = want or server_entry()
    rows = [status(t, want, path=(paths or {}).get(t)) for t in TOOLS]
    if check not in (None, "unknown"):
        for row in rows:
            if row["state"] == "connected":
                row.update(state="problem", reason=check)
    return {"tools": rows, "server": want, "check": check, "instruction": instruction(),
            "claude_code": claude_code_command(want)}


def claude_code_command(want: dict) -> str:
    """Claude Code keeps its servers itself: the command that adds this one (shown to copy)."""
    q = lambda x: f'"{x}"' if " " in x else x
    env = " ".join(f"--env {k}={q(v)}" for k, v in want["env"].items())
    return f"claude mcp add --scope user {env} {SERVER} -- {q(want['command'])} {' '.join(map(q, want['args']))}"
