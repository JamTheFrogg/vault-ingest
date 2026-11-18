import { describe, expect, it } from "vitest";
import { meaningLine, needed, type Item } from "./ai";
import { actions, confirmText, stateLine, type ToolRow } from "./connect";
import { meaningAvailable } from "./palette";

const row = (state: ToolRow["state"], more: Partial<ToolRow> = {}): ToolRow => ({
  id: "cursor", name: "Cursor", path: "/home/u/.cursor/mcp.json", state, reason: null, old: [],
  restart: "Cursor loads it by itself.", snippet: '{\n  "mcpServers": {}\n}\n', connectable: true, ...more,
});

describe("Connect AI tools", () => {
  it("says each state and offers its action", () => {
    expect(stateLine(row("not_installed"), false)).toEqual({ tone: "muted", text: "Not found on this computer" });
    expect(actions(row("not_installed"))).toEqual(["copy"]);
    expect(actions(row("not_connected"))).toEqual(["connect", "copy"]);
    expect(stateLine(row("connected"), true).text).toBe("Connected. Cursor loads it by itself.");
    expect(stateLine(row("connected"), false).text).toBe("Connected");
    expect(actions(row("connected"))).toEqual(["disconnect"]);
    expect(actions(row("changed"))).toEqual(["reconnect", "copy"]);
    const broken = row("error", { reason: "The file cannot be read: Expecting value: line 1 column 2" });
    expect(stateLine(broken, false)).toEqual({ tone: "failed",
      text: "Its config file has errors, so it was left alone. The file cannot be read: Expecting value: line 1 column 2" });
    expect(actions(broken)).toEqual(["copy"]);
    expect(stateLine(row("problem", { reason: "The vault server did not start: no VAULT_PATH" }), false).tone).toBe("failed");
  });

  it("offers only the copy where it cannot write (the AppImage)", () => {
    expect(actions(row("not_connected", { connectable: false }))).toEqual(["copy"]);
  });

  it("names the file before it writes", () => {
    expect(confirmText(row("not_connected"))).toContain("/home/u/.cursor/mcp.json");
  });
});

describe("search by meaning", () => {
  it("is offered once notes have vectors and the server runs", () => {
    expect(meaningAvailable(null)).toBe(false);
    expect(meaningAvailable({ state: "off", done: 0 })).toBe(false);
    expect(meaningAvailable({ state: "indexing", done: 0 })).toBe(false);
    expect(meaningAvailable({ state: "indexing", done: 12 })).toBe(true);
    expect(meaningAvailable({ state: "failed", done: 3000 })).toBe(false);
  });

  it("is never part of what the built-in AI must download", () => {
    const item = (id: string, kind: Item["kind"], optional = false): Item => ({ id, kind, title: id, note: null, size: 1,
      licence: "MIT", checked: "size", optional, state: "missing", done: 0, total: 1, speed: 0, message: null });
    const items = [item("llama-win-cuda", "llama-server"), item("gemma", "chat"), item("whisper", "whisper"),
      item("llama-win-cpu", "llama-server", true), item("bge-m3", "embedding", true)];
    expect(needed(items, "builtin", "gemma").map((i) => i.id)).toEqual(["llama-win-cuda", "gemma", "whisper"]);
  });

  it("says how far the notes are", () => {
    expect(meaningLine({ state: "off", done: 0, total: 0, message: null })).toBeNull();
    expect(meaningLine({ state: "indexing", done: 1204, total: 3000, message: null })?.text)
      .toBe("Preparing notes for search by meaning: 1,204 of 3,000");
    expect(meaningLine({ state: "ready", done: 3000, total: 3000, message: null })).toEqual({ tone: "done", text: "Search by meaning is ready for all 3,000 notes." });
  });
});
