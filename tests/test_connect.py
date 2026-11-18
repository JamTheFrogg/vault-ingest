import dataclasses
import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from vault_ingest import connect
from vault_ingest.app import Engine
from vault_ingest.config import PROJECT_DIR
from vault_ingest.connect import ConnectError
from vault_ingest.server import make_app
from vault_ingest.watcher_meetings import load_config

WANT = {"command": r"C:\Program Files\vault-ingest\engine\python.exe", "args": ["-I", "-m", "vault_ingest.mcp_server"],
        "env": {"VAULT_INGEST_HOME": r"C:\Users\someone\AppData\Roaming\vault-ingest"}}
OLD = {"command": r"C:\tools\vault-ingest\bin\obsidian-mcp.exe", "env": {"OBSIDIAN_VAULT_PATH": r"C:\Vault"}}
CLAUDE = {"globalShortcut": "Ctrl+Space", "mcpServers": {"obsidian": OLD, "github": {"command": "npx", "args": ["gh"]}}}
CODEX = """# my Codex settings
model = "gpt-6"

[mcp_servers.obsidian]
command = 'C:\\tools\\vault-ingest\\bin\\obsidian-mcp.exe'

[mcp_servers.obsidian.env]
OBSIDIAN_VAULT_PATH = 'C:\\Vault'

[mcp_servers.docs]
command = "docs-server"   # keep this comment
args = [
  "--port", "9",
]

[profiles.fast]
model = "gpt-6-mini"
"""


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".bak-*"))


def test_config_files_per_system(tmp_path):
    home = tmp_path / "u"
    env = {"APPDATA": str(home / "Roaming"), "LOCALAPPDATA": str(home / "Local")}
    path = connect.config_path("claude", os_name="windows", env=env, home_dir=home)
    assert path == home / "Roaming/Claude/claude_desktop_config.json"
    store = home / "Local/Packages/Claude_abc123/LocalCache/Roaming/Claude"
    store.mkdir(parents=True)                                     # only the Microsoft Store folder exists
    assert connect.config_path("claude", os_name="windows", env=env, home_dir=home) == store / "claude_desktop_config.json"
    (home / "Roaming/Claude").mkdir(parents=True)                 # the regular install wins when both exist
    assert connect.config_path("claude", os_name="windows", env=env, home_dir=home) == path
    assert connect.config_path("claude", os_name="macos", env={}, home_dir=home) == \
        home / "Library/Application Support/Claude/claude_desktop_config.json"
    assert connect.config_path("claude", os_name="linux", env={}, home_dir=home) == home / ".config/Claude/claude_desktop_config.json"
    assert connect.config_path("cursor", os_name="windows", env=env, home_dir=home) == home / ".cursor/mcp.json"
    assert connect.config_path("codex", os_name="linux", env={}, home_dir=home) == home / ".codex/config.toml"
    assert connect.config_path("codex", os_name="linux", env={"CODEX_HOME": str(tmp_path / "c")}, home_dir=home) == \
        tmp_path / "c/config.toml"


def test_a_tool_that_is_not_installed_and_a_first_connection(tmp_path):
    path = tmp_path / ".cursor" / "mcp.json"
    assert connect.status("cursor", WANT, path=path, appimage=False)["state"] == "not_installed"
    path.parent.mkdir()
    row = connect.status("cursor", WANT, path=path, appimage=False)
    assert row["state"] == "not_connected" and row["old"] == [] and json.loads(row["snippet"]) == {"mcpServers": {"vault": WANT}}
    assert connect.connect("cursor", WANT, path=path) == {"path": str(path), "backup": None}
    assert json.loads(path.read_text()) == {"mcpServers": {"vault": WANT}}
    assert connect.status("cursor", WANT, path=path, appimage=False)["state"] == "connected"


def test_claude_desktop_keeps_its_settings_and_other_servers(tmp_path):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir()
    original = json.dumps(CLAUDE, indent=4)
    path.write_text(original, encoding="utf-8")
    row = connect.status("claude", WANT, path=path, appimage=False)
    assert row["state"] == "not_connected" and row["old"] == ["obsidian"]
    done = connect.connect("claude", WANT, path=path)
    data = json.loads(path.read_text())
    assert data == {**CLAUDE, "mcpServers": {**CLAUDE["mcpServers"], "vault": WANT}}       # the old entry stays
    assert Path(done["backup"]).read_text() == original and backups(path) == [Path(done["backup"])]
    with pytest.raises(ConnectError):
        connect.remove_old("claude", "github", path=path)             # only an old obsidian entry, on request
    connect.remove_old("claude", "obsidian", path=path)
    assert json.loads(path.read_text())["mcpServers"] == {"github": CLAUDE["mcpServers"]["github"], "vault": WANT}
    connect.disconnect("claude", path=path)
    assert json.loads(path.read_text()) == {"globalShortcut": "Ctrl+Space",
                                            "mcpServers": {"github": CLAUDE["mcpServers"]["github"]}}
    assert len(backups(path)) == 3                                    # one before every write


def test_codex_toml_is_edited_as_text(tmp_path):
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(CODEX, encoding="utf-8")
    assert connect.status("codex", WANT, path=path, appimage=False)["old"] == ["obsidian"]
    connect.connect("codex", WANT, path=path)
    text = path.read_text()
    data = tomllib.loads(text)
    assert data["mcp_servers"]["vault"] == WANT                      # Windows paths survive as literal strings
    assert text.startswith(CODEX.rstrip("\n")) and "# keep this comment" in text
    assert data["profiles"]["fast"]["model"] == "gpt-6-mini" and data["mcp_servers"]["docs"]["args"] == ["--port", "9"]
    # connecting again with another interpreter replaces the block, nothing is added twice
    moved = {**WANT, "command": "/opt/vault-ingest/engine/bin/python3"}
    assert connect.status("codex", moved, path=path, appimage=False)["state"] == "changed"
    connect.connect("codex", moved, path=path)
    assert path.read_text().count("[mcp_servers.vault]") == 1
    assert tomllib.loads(path.read_text())["mcp_servers"]["vault"]["command"] == moved["command"]
    connect.remove_old("codex", "obsidian", path=path)
    connect.disconnect("codex", path=path)
    left = tomllib.loads(path.read_text())
    assert set(left["mcp_servers"]) == {"docs"} and left["model"] == "gpt-6" and "profiles" in left
    assert len(backups(path)) == 4


def test_a_file_the_tool_cannot_read_is_never_changed(tmp_path):
    path = tmp_path / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text('{"mcpServers": {"a": {"command": "x"},}', encoding="utf-8")       # a trailing comma
    row = connect.status("cursor", WANT, path=path, appimage=False)
    assert row["state"] == "error" and "cannot be read" in row["reason"]
    with pytest.raises(ConnectError):
        connect.connect("cursor", WANT, path=path)
    assert path.read_text() == '{"mcpServers": {"a": {"command": "x"},}' and backups(path) == []
    # a TOML layout this app does not edit: refused, untouched
    codex = tmp_path / "config.toml"
    codex.write_text('mcp_servers.vault = { command = "x" }\n', encoding="utf-8")
    with pytest.raises(ConnectError):
        connect.connect("codex", WANT, path=codex)
    assert codex.read_text() == 'mcp_servers.vault = { command = "x" }\n' and backups(codex) == []


def test_the_appimage_offers_only_the_copy(tmp_path):
    (tmp_path / ".cursor").mkdir()
    row = connect.status("cursor", WANT, path=tmp_path / ".cursor/mcp.json", appimage=True)
    assert not row["connectable"] and ".deb" in row["reason"] and row["snippet"]


def test_the_server_check_runs_the_tools_command(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw["env"]["VAULT_INGEST_HOME"]))
        return subprocess.CompletedProcess(argv, 1, "", "Traceback ...\nSystemExit: VAULT_PATH is not set.")

    assert connect.check_server(WANT, run) == "The vault server did not start: SystemExit: VAULT_PATH is not set."
    assert calls == [([*[WANT["command"]], *WANT["args"], "--check"], WANT["env"]["VAULT_INGEST_HOME"])]
    assert connect.check_server(WANT, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "ok", "")) is None


def test_the_entry_uses_this_interpreter_and_data_folder(tmp_path):
    entry = connect.server_entry(data_home=tmp_path)
    assert entry["args"][-2:] == ["-m", "vault_ingest.mcp_server"] and entry["env"] == {"VAULT_INGEST_HOME": str(tmp_path)}
    assert '"vault" MCP tools' in connect.instruction() and "obsidian" not in connect.instruction()


# ------------------------------------------------------------------ the API
@pytest.fixture
def api(vault, settings, tmp_path, monkeypatch):
    cfg = dataclasses.replace(load_config(PROJECT_DIR / "companion.toml"), screenshot_folder=vault / "nope")
    engine = Engine(settings, cfg, data_dir=tmp_path / "home")
    (tmp_path / "cfg/.cursor").mkdir(parents=True)
    (tmp_path / "cfg/Claude").mkdir(parents=True)
    (tmp_path / "cfg/Claude/claude_desktop_config.json").write_text(json.dumps(CLAUDE), encoding="utf-8")
    engine.connect_paths = {"claude": tmp_path / "cfg/Claude/claude_desktop_config.json",
                            "cursor": tmp_path / "cfg/.cursor/mcp.json", "codex": tmp_path / "cfg/.codex/config.toml"}
    monkeypatch.setattr(connect, "check_server", lambda want: None)
    monkeypatch.delenv("APPIMAGE", raising=False)
    return TestClient(make_app(engine, "t"), base_url="http://127.0.0.1"), {"Authorization": "Bearer t"}, tmp_path


def test_connect_endpoints(api):
    client, auth, tmp_path = api
    data = client.get("/connect", headers=auth).json()
    assert [(t["id"], t["state"], t["old"]) for t in data["tools"]] == [
        ("claude", "not_connected", ["obsidian"]), ("cursor", "not_connected", []), ("codex", "not_installed", [])]
    assert data["server"]["args"][-2:] == ["-m", "vault_ingest.mcp_server"] and '"vault" MCP tools' in data["instruction"]
    assert data["claude_code"].startswith("claude mcp add --scope user --env VAULT_INGEST_HOME=") and \
        data["claude_code"].endswith(" vault -- " + " ".join([data["server"]["command"], *data["server"]["args"]]))
    r = client.post("/connect/cursor", headers=auth, json={"action": "connect"})
    assert r.status_code == 200 and r.json()["done"]["backup"] is None
    assert [t["state"] for t in r.json()["tools"]] == ["not_connected", "connected", "not_installed"]
    r = client.post("/connect/claude", headers=auth, json={"action": "remove_old", "name": "obsidian"})
    assert r.json()["tools"][0]["old"] == [] and r.json()["done"]["backup"]
    assert client.post("/connect/zed", headers=auth, json={"action": "connect"}).status_code == 404
    assert client.post("/connect/cursor", headers=auth, json={"action": "explode"}).status_code == 400
    (tmp_path / "cfg/.cursor/mcp.json").write_text("{", encoding="utf-8")
    r = client.post("/connect/cursor", headers=auth, json={"action": "connect"})
    assert r.status_code == 409 and "cannot be read" in r.json()["error"]
    assert client.get("/connect").status_code == 401
