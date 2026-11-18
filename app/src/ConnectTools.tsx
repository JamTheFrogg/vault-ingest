// Settings > Connect AI tools: one row per tool with its state, Connect / Disconnect, and the config to copy.
import { CheckCircle, Circle, Copy, MinusCircle, Plug, WarningCircle } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { actions, confirmText, stateLine, type Connections, type ToolRow } from "./connect";
import type { Api } from "./engine";

function StateIcon({ tone, row }: { tone: string; row: ToolRow }) {
  if (tone === "done") return <CheckCircle size={14} weight="fill" aria-hidden />;
  if (tone === "failed" || tone === "paused") return <WarningCircle size={14} weight="fill" aria-hidden />;
  return row.state === "not_installed" ? <MinusCircle size={14} aria-hidden /> : <Circle size={14} aria-hidden />;
}

/** A button that copies `text` and says so for a moment. */
function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  const copy = () => navigator.clipboard.writeText(text).then(() => setCopied(true), () => setCopied(false))
    .finally(() => window.setTimeout(() => setCopied(null), 2000));
  return (
    <button className="pbutton" onClick={copy}>
      <Copy size={14} aria-hidden />
      <span aria-live="polite">{copied === null ? label : copied ? "Copied" : "Could not copy"}</span>
    </button>
  );
}

export function ConnectSettings({ api }: { api: Api | null }) {
  const [data, setData] = useState<Connections | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [asking, setAsking] = useState<string | null>(null);         // the tool whose Connect waits for a yes
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => api?.get<Connections>("/connect").then((d) => { setData(d); setError(null); },
    (e) => setError((e as Error).message)), [api]);
  useEffect(() => { load(); }, [load]);

  const act = async (tool: string, action: string, name?: string) => {
    if (!api) return;
    setBusy(`${tool}:${action}`);
    try {
      setData(await api.post<Connections>(`/connect/${tool}`, { action, name }));
      setError(null);
      setFresh((f) => { const next = new Set(f); if (action === "connect") next.add(tool); else next.delete(tool); return next; });
      setAsking(null);
    } catch (e) {
      setError(`${data?.tools.find((t) => t.id === tool)?.name ?? tool}: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  if (!data) {
    return <>{error && <p className="settings-error tone-failed" role="alert"><WarningCircle size={16} weight="fill" aria-hidden />{error}</p>}
      <div className="skeleton detail-skeleton" aria-label="Loading"><div /><div /><div /></div></>;
  }
  const anyConnected = data.tools.some((t) => t.state === "connected" || t.state === "problem");
  const found = data.tools.some((t) => t.state !== "not_installed");
  return (
    <>
      {error && <p className="settings-error tone-failed" role="alert"><WarningCircle size={16} weight="fill" aria-hidden />{error}</p>}
      <section className="sgroup" aria-labelledby="g-tools">
        <div className="group-head"><h2 id="g-tools">Your AI tools</h2></div>
        <p className={`test-line tone-${!anyConnected ? "muted" : data.check ? "failed" : "done"}`}>
          {anyConnected && (data.check ? <WarningCircle size={14} weight="fill" aria-hidden /> : <CheckCircle size={14} weight="fill" aria-hidden />)}
          {anyConnected && data.check ? data.check
            : anyConnected ? "The vault server starts and answers. Each tool starts it itself, also while this app is closed."
              : "Each tool starts the vault server itself, also while this app is closed."}
        </p>
        {!found && <p className="choice-note">No supported AI tool was found on this computer. Copy the config into the tool by hand.</p>}
        <ul className="tools" aria-label="AI tools">
          {data.tools.map((row) => {
            const line = stateLine(row, fresh.has(row.id));
            return (
              <li key={row.id} className="tool">
                <span className="tool-name">{row.name}</span>
                <span className="tool-main">
                  <span className={`model-state tone-${line.tone}`}><StateIcon tone={line.tone} row={row} />{line.text}</span>
                  {row.state !== "not_installed" && <span className="path tool-path">{row.path}</span>}
                  {!row.connectable && row.reason && <span className="choice-note">{row.reason}</span>}
                  {row.old.map((name) => (
                    <span key={name} className="tool-old">
                      <span>An old “{name}” entry from the earlier setup is still in this file. Your tools may show both.</span>
                      <button className="link-button accent" disabled={busy !== null}
                        onClick={() => act(row.id, "remove_old", name)}>Remove “{name}”</button>
                    </span>
                  ))}
                </span>
                <span className="tool-actions">
                  {actions(row).map((a) => a === "copy" ? <CopyButton key={a} text={row.snippet} label="Copy the config" />
                    : a === "disconnect" ? <button key={a} className="pbutton" disabled={busy !== null}
                      onClick={() => act(row.id, "disconnect")}>Disconnect</button>
                    : <button key={a} className="pbutton" disabled={busy !== null || asking === row.id}
                      onClick={() => setAsking(row.id)}><Plug size={14} aria-hidden />{a === "reconnect" ? "Connect again" : "Connect"}</button>)}
                </span>
                {asking === row.id && (
                  <div className="tool-confirm" role="group" aria-label={`Connect ${row.name}`}>
                    <p className="choice-note">{confirmText(row)}</p>
                    <pre className="snippet">{row.snippet}</pre>
                    <div className="tool-actions">
                      <button className="button primary" disabled={busy !== null} onClick={() => act(row.id, "connect")}>
                        <Plug size={16} aria-hidden />Add it</button>
                      <button className="button" onClick={() => setAsking(null)}>Cancel</button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
          <li className="tool">
            <span className="tool-name">Claude Code</span>
            <span className="tool-main">
              <span className="model-state tone-muted">Keeps its servers itself. Run this once in a terminal:</span>
              <code className="tool-command">{data.claude_code}</code>
            </span>
            <span className="tool-actions"><CopyButton text={data.claude_code} label="Copy the command" /></span>
          </li>
        </ul>
      </section>

      <section className="sgroup" aria-labelledby="g-instruction">
        <div className="group-head"><h2 id="g-instruction">Tell each tool to use the vault</h2><span className="muted">optional, once</span></div>
        <blockquote className="instruction">{data.instruction}</blockquote>
        <div className="test-row">
          <CopyButton text={data.instruction} label="Copy the text" />
          <span className="choice-note">Paste it into Claude Desktop: Settings, personal preferences · Cursor: Rules, User Rules ·
            Claude Code: ~/.claude/CLAUDE.md · Codex: ~/.codex/AGENTS.md</span>
        </div>
      </section>
    </>
  );
}
