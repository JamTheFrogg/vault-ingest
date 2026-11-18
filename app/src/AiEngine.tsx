// Settings > AI engine and the AI step of first-run setup (plan/03-ux-shape.md 3.8 and 5, step 3).
// Both read GET /settings/ai and follow download.progress and ai.test on /events.
import { ArrowClockwise, CheckCircle, DownloadSimple, Pause, Play, Waveform, WarningCircle } from "@phosphor-icons/react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  ADDRESSES, applyProgress, bytes, itemAction, itemLine, machine, meaningLine, missing, needed, overall, testLine,
  type AiSettings, type Embedding, type Engine, type Item, type Progress, type TestResult,
} from "./ai";
import { Api, engineInfo } from "./engine";
import { Mark } from "./Rail";

const FOR: Record<Item["kind"], string> = {
  "llama-server": "The engine", chat: "Notes", whisper: "Speech to text", "gpu-libs": "Graphics card", embedding: "Search by meaning",
};
// rows only search by meaning needs sit under its label, also the processor build it runs on
const forLabel = (i: Item) => (i.optional ? FOR.embedding : FOR[i.kind]);
const ENGINES: { id: Engine; name: string; text: string }[] = [
  { id: "builtin", name: "Built-in", text: "Runs on this computer. Nothing leaves it." },
  { id: "lmstudio", name: "LM Studio", text: "A model you already run in LM Studio." },
  { id: "ollama", name: "Ollama", text: "A model you already run in Ollama." },
  { id: "cloud", name: "Claude API", text: "Best notes. Meeting text is sent to Anthropic." },
];

export type AiChange = { engine: Engine; model?: string; address?: string; api_key?: string };

/** The AI settings, kept current from the event stream. */
export function useAi(api: Api | null) {
  const [ai, setAi] = useState<AiSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => api?.get<AiSettings>("/settings/ai").then((a) => { setAi(a); setError(null); },
    (e) => setError(String((e as Error).message))), [api]);
  useEffect(() => {
    if (!api) return;
    load();
    const stream = api.events();
    stream.addEventListener("download.progress", (e) => {
      const p = JSON.parse((e as MessageEvent).data) as Progress;
      setAi((a) => (a ? { ...a, items: applyProgress(a.items, p) } : a));
      if (p.state === "done") load();                      // free space, and what is installed now
    });
    stream.addEventListener("ai.test", (e) => {
      const test = JSON.parse((e as MessageEvent).data) as TestResult;
      setAi((a) => (a ? { ...a, test } : a));
    });
    stream.addEventListener("embeddings.progress", (e) => {
      const embedding = JSON.parse((e as MessageEvent).data) as Embedding;
      setAi((a) => (a ? { ...a, embedding } : a));
    });
    stream.addEventListener("settings.changed", () => load());
    return () => stream.close();
  }, [api, load]);

  const change = async (body: AiChange) => {
    if (!api) return false;
    try {
      setAi(await api.patch<AiSettings>("/settings/ai", body));
      setError(null);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  };
  const download = (id: string, action: "start" | "cancel") => api?.post<Progress>(`/settings/ai/downloads/${id}`, { action })
    .then((p) => setAi((a) => (a ? { ...a, items: applyProgress(a.items, p) } : a)), (e) => setError((e as Error).message));
  const test = () => api?.post<TestResult>("/settings/ai/test").then((t) => setAi((a) => (a ? { ...a, test: t } : a)),
    (e) => setError((e as Error).message));
  return { ai, error, change, download, test };
}

function Problem({ text }: { text: string }) {
  return <p className="settings-error tone-failed" role="alert"><WarningCircle size={16} weight="fill" aria-hidden />{text}</p>;
}

// ------------------------------------------------------------------ Settings > AI engine
export function AiEngineSettings({ api }: { api: Api | null }) {
  const { ai, error, change, download, test } = useAi(api);
  if (!ai) {
    return <>{error && <Problem text={error} />}<div className="skeleton detail-skeleton" aria-label="Loading"><div /><div /><div /></div></>;
  }
  const want = needed(ai.items, ai.engine, ai.model);
  const left = missing(want);
  const line = testLine(ai.test);
  const builtinMissing = ai.engine === "builtin" && want.some((i) => i.kind !== "gpu-libs" && i.state !== "done");
  const meaning = meaningLine(ai.embedding);
  return (
    <>
      {error && <Problem text={error} />}
      <section className="sgroup" aria-labelledby="g-engine">
        <div className="group-head"><h2 id="g-engine">Who writes the notes</h2></div>
        <EngineChoices ai={ai} onChange={change} />
        {ai.pending && <p className="choice-note tone-paused">Takes effect when the meeting being written now is done.</p>}
        <div className="test-row">
          <button className="button" onClick={test} disabled={ai.test?.running || builtinMissing}>
            <Waveform size={16} aria-hidden />Test my setup</button>
          {builtinMissing && !line && <span className="choice-note">Download the built-in AI first.</span>}
          {line && <span className={`test-line tone-${line.tone}`} aria-live="polite">
            {line.tone === "done" ? <CheckCircle size={14} weight="fill" aria-hidden />
              : line.tone !== "working" ? <WarningCircle size={14} weight="fill" aria-hidden /> : null}{line.text}</span>}
        </div>
      </section>

      <section className="sgroup" aria-labelledby="g-models">
        <div className="group-head"><h2 id="g-models">Models on this computer</h2>
          <span className="muted">{machine(ai.hardware)}</span></div>
        <ModelRows items={ai.items} onAct={download} />
        <div className="models-foot">
          {left > 0 && (
            <button className="button primary" onClick={() => want.filter((i) => itemAction(i) !== "pause" && i.state !== "done")
              .forEach((i) => download(i.id, "start"))}>
              <DownloadSimple size={16} aria-hidden />Download what {ai.engine === "builtin" ? "the built-in AI" : "speech to text"} needs · {bytes(left)}</button>
          )}
          {meaning && <p className={`test-line tone-${meaning.tone}`} aria-live="polite">
            {meaning.tone === "done" ? <CheckCircle size={14} weight="fill" aria-hidden />
              : meaning.tone !== "working" ? <WarningCircle size={14} weight="fill" aria-hidden /> : null}{meaning.text}</p>}
          <p className="choice-note">Kept in <span className="path">{ai.models_dir}</span> · {bytes(ai.free_bytes)} free</p>
        </div>
      </section>
    </>
  );
}

function EngineChoices({ ai, onChange }: { ai: AiSettings; onChange: (c: AiChange) => Promise<boolean> }) {
  const [address, setAddress] = useState<Record<"lmstudio" | "ollama", string>>(() => ({
    lmstudio: ai.engine === "lmstudio" && ai.address ? ai.address : ADDRESSES.lmstudio,
    ollama: ai.engine === "ollama" && ai.address ? ai.address : ADDRESSES.ollama,
  }));
  const [key, setKey] = useState("");
  const [picked, setPicked] = useState<Engine>(ai.engine);
  const chats = ai.items.filter((i) => i.kind === "chat");

  const choose = (engine: Engine) => {
    setPicked(engine);
    if (engine === "cloud" && !ai.key_hint) return;          // asks for the key first
    onChange(engine === "builtin" ? { engine, model: ai.model } : engine === "cloud" ? { engine }
      : { engine, address: address[engine] });
  };
  return (
    <div className="engines" role="radiogroup" aria-labelledby="g-engine">
      {ENGINES.map((e) => (
        <div key={e.id} className="engine">
          <label className="choice">
            <input type="radio" name="engine" checked={picked === e.id} onChange={() => choose(e.id)} />
            <span><span className="engine-name">{e.name}</span><span className="choice-note">{e.text}</span></span>
          </label>
          {picked === e.id && (
            <div className="engine-field">
              {e.id === "builtin" && (
                <label className="field"><span>Model</span>
                  <select className="select plain" value={ai.model} onChange={(ev) => onChange({ engine: "builtin", model: ev.target.value })}>
                    {chats.map((c) => <option key={c.id} value={c.id}>{c.title} · {bytes(c.size)}{c.id === ai.hardware.recommend ? " · suits this computer" : ""}</option>)}
                  </select></label>
              )}
              {(e.id === "lmstudio" || e.id === "ollama") && (
                <label className="field"><span>Address</span>
                  <input className="input" value={address[e.id]} spellCheck={false}
                    onChange={(ev) => setAddress({ ...address, [e.id]: ev.target.value })}
                    onBlur={() => onChange({ engine: e.id, address: address[e.id as "lmstudio" | "ollama"] })}
                    onKeyDown={(ev) => { if (ev.key === "Enter") (ev.target as HTMLInputElement).blur(); }} /></label>
              )}
              {e.id === "cloud" && (
                <form className="field" onSubmit={(ev) => { ev.preventDefault(); onChange({ engine: "cloud", api_key: key }).then((ok) => ok && setKey("")); }}>
                  <span>Key</span>
                  <input className="input" type="password" value={key} autoComplete="off" spellCheck={false}
                    placeholder={ai.key_hint ? `Saved key ${ai.key_hint}` : "Paste your Anthropic API key"}
                    onChange={(ev) => setKey(ev.target.value)} />
                  <button className="button" type="submit" disabled={!key.trim()}>{ai.key_hint ? "Replace key" : "Use this key"}</button>
                </form>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function ModelRows({ items, onAct }: { items: Item[]; onAct: (id: string, action: "start" | "cancel") => void }) {
  return (
    <ul className="models" aria-label="Downloads">
      {items.map((i, n) => {
        const action = itemAction(i);
        const first = n === 0 || forLabel(items[n - 1]) !== forLabel(i);
        const pct = i.total ? Math.min(100, (i.done / i.total) * 100) : 0;
        return (
          <li key={i.id} className={`model${first ? " first" : ""}`}>
            <span className="model-for">{first ? forLabel(i) : ""}</span>
            <span className="model-main">
              <span className="model-name">{i.title}</span>
              <span className={`model-state tone-${i.state === "done" ? "done" : i.state === "failed" ? "failed"
                : i.state === "waiting" || i.state === "paused" ? "paused" : i.state === "missing" || i.state === "queued" ? "muted" : "working"}`}>
                {i.state === "done" && <CheckCircle size={14} weight="fill" aria-hidden />}
                {i.state === "failed" && <WarningCircle size={14} weight="fill" aria-hidden />}
                {itemLine(i)}</span>
              {(i.state === "downloading" || i.state === "waiting" || i.state === "paused") && (
                <span className="bar" role="progressbar" aria-label={`${i.title} download`} aria-valuemin={0} aria-valuemax={100}
                  aria-valuenow={Math.round(pct)}><span style={{ width: `${pct}%` }} /></span>
              )}
            </span>
            <span className="model-size num">{bytes(i.size)}</span>
            <span className="model-action">
              {action && <RowAction action={action} onClick={() => onAct(i.id, action === "pause" ? "cancel" : "start")} />}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function RowAction({ action, onClick }: { action: NonNullable<ReturnType<typeof itemAction>>; onClick: () => void }) {
  const actions: Record<typeof action, [ReactNode, string]> = {
    download: [<DownloadSimple size={14} aria-hidden />, "Download"],
    pause: [<Pause size={14} aria-hidden />, "Pause"],
    resume: [<Play size={14} aria-hidden />, "Resume"],
    retry: [<ArrowClockwise size={14} aria-hidden />, "Try again"],
  };
  const [icon, label] = actions[action];
  return <button className="pbutton" onClick={onClick}>{icon}{label}</button>;
}

// ------------------------------------------------------------------ first-run setup, step 3
/** The AI step of first-run setup as a screen of its own; the onboarding flow puts it after the sound check. */
export function AiSetup({ api, step = [3, 3], onBack, onFinish }: { api: Api | null; step?: [number, number];
  onBack?: () => void; onFinish: () => void }) {
  const { ai, error, change, download } = useAi(api);
  const [choice, setChoice] = useState<"builtin" | "other" | null>(null);
  const [other, setOther] = useState<Exclude<Engine, "builtin">>("cloud");
  const [address, setAddress] = useState(ADDRESSES);
  const [key, setKey] = useState("");
  const [model, setModel] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  if (!ai) {
    return <div className="setup">{error && <Problem text={error} />}<div className="skeleton detail-skeleton" aria-label="Loading"><div /><div /><div /></div></div>;
  }
  const hw = ai.hardware;
  const pick = choice ?? (hw.recommend === "cloud" ? "other" : "builtin");
  const chosen = model ?? (hw.recommend === "cloud" ? ai.light_chat : hw.recommend);
  const engine: Engine = pick === "builtin" ? "builtin" : other;
  const want = needed(ai.items, engine, chosen);
  const speech = ai.items.filter((i) => i.kind === "whisper" || i.kind === "gpu-libs");
  const builtinSize = missing(needed(ai.items, "builtin", chosen));
  const short = builtinSize > ai.free_bytes;
  const progress = overall(want);

  const finish = async () => {
    const body: AiChange = engine === "builtin" ? { engine, model: chosen } : engine === "cloud" ? { engine, api_key: key || undefined }
      : { engine, address: address[engine] };
    if (!(await change(body))) return;
    want.filter((i) => i.state !== "done").forEach((i) => download(i.id, "start"));
    setStarted(true);
  };

  return (
    <section className="setup" aria-labelledby="setup-title">
      <header className="setup-head">
        <div className="setup-mark"><span><Mark />vault-ingest</span><span className="muted num">{step[0]} of {step[1]}</span></div>
        <h1 id="setup-title" className="page-title">Who writes the notes?</h1>
      </header>
      {error && <Problem text={error} />}
      {started ? (
        <div className="setup-body">
          <p className={`setup-progress tone-${progress.state === "failed" ? "failed" : progress.state === "done" ? "done" : "working"}`}
            aria-live="polite">{progress.line || "Saved."}</p>
          <ModelRows items={want} onAct={download} />
          <p className="choice-note">You can start using the app while it downloads. Recordings are kept and get their notes
            as soon as the AI is there.</p>
        </div>
      ) : (
        <div className="setup-body" role="radiogroup" aria-labelledby="setup-title">
          <label className="choice big">
            <input type="radio" name="setup" checked={pick === "builtin"} onChange={() => setChoice("builtin")} />
            <span>
              <span className="engine-name">Download the built-in AI <span className="muted num">{bytes(builtinSize)}</span></span>
              <span className="choice-note">Runs on this computer. Nothing leaves it.</span>
              <span className="choice-note">{hw.reason}
                {" "}Free disk space: {bytes(ai.free_bytes)}.</span>
              {short && <span className="choice-note tone-failed">Needs {bytes(builtinSize - ai.free_bytes)} more free space.</span>}
            </span>
          </label>
          {pick === "builtin" && (
            <div className="engine-field">
              <label className="field"><span>Model</span>
                <select className="select plain" value={chosen} onChange={(e) => setModel(e.target.value)}>
                  {ai.items.filter((i) => i.kind === "chat").map((c) => <option key={c.id} value={c.id}>
                    {c.title} · {bytes(c.size)}{c.id === hw.recommend ? " · suits this computer" : ""}</option>)}
                </select></label>
              <p className="choice-note">You can start using the app while it downloads.</p>
            </div>
          )}
          <label className="choice big">
            <input type="radio" name="setup" checked={pick === "other"} onChange={() => setChoice("other")} />
            <span><span className="engine-name">Use something I already have</span>
              <span className="choice-note">Speech to text still runs on this computer and needs a {bytes(missing(speech))} download.</span></span>
          </label>
          {pick === "other" && (
            <div className="engine-field others" role="radiogroup" aria-label="What you already have">
              {(["lmstudio", "ollama", "cloud"] as const).map((id) => (
                <div key={id} className="other">
                  <label className="choice">
                    <input type="radio" name="other" checked={other === id} onChange={() => setOther(id)} />
                    <span className="engine-name">{ENGINES.find((e) => e.id === id)!.name}</span>
                  </label>
                  {id === "cloud" ? (
                    <input className="input" type="password" value={key} autoComplete="off" spellCheck={false} aria-label="Anthropic API key"
                      placeholder={ai.key_hint ? `Saved key ${ai.key_hint}` : "Paste a key"} onFocus={() => setOther(id)}
                      onChange={(e) => setKey(e.target.value)} />
                  ) : (
                    <input className="input" value={address[id]} spellCheck={false} aria-label={`${id === "lmstudio" ? "LM Studio" : "Ollama"} address`}
                      onFocus={() => setOther(id)} onChange={(e) => setAddress({ ...address, [id]: e.target.value })} />
                  )}
                </div>
              ))}
              {other === "cloud" && <p className="choice-note">The meeting text is sent to Anthropic to write each note.</p>}
            </div>
          )}
        </div>
      )}
      <footer className="setup-foot">
        {onBack && !started && <button className="link-button" onClick={onBack}>Back</button>}
        {started ? <button className="button primary" onClick={onFinish}>Open vault-ingest</button>
          : <button className="button primary" onClick={finish}
            disabled={(pick === "builtin" && short) || (engine === "cloud" && !key.trim() && !ai.key_hint)}>Finish</button>}
      </footer>
    </section>
  );
}

/** The step as a window of its own (`?view=setup-ai`), with its own connection to the engine. */
export function SetupAiScreen() {
  const [api, setApi] = useState<Api | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    engineInfo().then((info) => (info.state === "ready" ? setApi(new Api(info)) : setError(info.error)));
  }, []);
  return (
    <main className="setup-screen">
      {error ? <Problem text={error} /> : <AiSetup api={api} onFinish={() => location.assign(location.pathname)} />}
    </main>
  );
}
