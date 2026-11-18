// Settings > Connect AI tools (plan/03-ux-shape.md 3.8): what GET /connect reports per tool, and the words for it.

export type ToolState = "not_installed" | "not_connected" | "connected" | "changed" | "error" | "problem";
export type ToolRow = {
  id: "claude" | "cursor" | "codex"; name: string; path: string; state: ToolState; reason: string | null;
  old: string[]; restart: string; snippet: string; connectable: boolean;
};
export type Connections = {
  tools: ToolRow[]; server: { command: string; args: string[]; env: Record<string, string> };
  check: string | null; instruction: string; claude_code: string;
  done?: { tool: string; action: string; path: string; backup: string | null };
};
export type Action = "connect" | "disconnect" | "reconnect" | "copy";

/** The state as the row says it; `fresh` right after Connect, when the tool still has to load it. */
export function stateLine(row: ToolRow, fresh: boolean): { tone: string; text: string } {
  switch (row.state) {
    case "not_installed": return { tone: "muted", text: "Not found on this computer" };
    case "not_connected": return { tone: "muted", text: "Not connected" };
    case "connected": return fresh ? { tone: "done", text: `Connected. ${row.restart}` } : { tone: "done", text: "Connected" };
    case "changed": return { tone: "paused", text: row.reason ?? "Its entry starts another program." };
    case "problem": return { tone: "failed", text: row.reason ?? "The vault server does not start." };
    case "error": return { tone: "failed", text: `Its config file has errors, so it was left alone. ${row.reason ?? ""}`.trim() };
  }
}

/** The buttons a row offers, the main one first. Copying the config is always there except once connected. */
export function actions(row: ToolRow): Action[] {
  const copy: Action[] = ["copy"];
  if (!row.connectable) return copy;
  switch (row.state) {
    case "not_connected": return ["connect", ...copy];
    case "changed": return ["reconnect", ...copy];
    case "connected": return ["disconnect"];
    case "problem": return ["disconnect", ...copy];
    default: return copy;
  }
}

/** What Connect does, said before anything is written. */
export function confirmText(row: ToolRow): string {
  return `This adds the vault entry below to ${row.path}. A backup copy goes next to the file first; nothing else in it changes.`;
}
