// Settings (plan/03-ux-shape.md section 3.8): Recording here, AI engine and Connect AI tools in their own files.
// Changes apply at once; no Save button.
import { CheckCircle, Microphone, WarningCircle } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { AiEngineSettings } from "./AiEngine";
import { ConnectSettings } from "./ConnectTools";
import type { Api } from "./engine";
import { LevelStrip } from "./Level";
import { HEARD, micCheck, pushLevel } from "./recording";
import { micDevices, micLevels, micPreview, micSelect, platform, type Device, type Meter, type MicChoice, type Platform } from "./shell";

type Hotkeys = Record<"popup" | "screenshot" | "record" | "mark" | "pause", string>;
export type RecordingSettings = {
  auto_record: boolean; hide_popup_from_sharing: boolean; mic: MicChoice; recordings_folder: string;
  hotkeys: Hotkeys; shortcut_failures: Record<string, string>;
};

const STRIP = 48;
const ACTIONS: [keyof Hotkeys, string][] = [
  ["popup", "Show the call popup and give it the keyboard"],
  ["screenshot", "Take a screenshot (region)"],
  ["record", "Start or stop recording"],
  ["mark", "Mark this moment"],
  ["pause", "Pause or resume AI work"],
];

function choiceValue(c: MicChoice): string {
  return c.kind === "device" ? `device:${c.id}` : c.kind;
}

/** `ctrl+alt+r` as the keys read on this system. */
export function keyLabel(combo: string, os: string): string {
  if (!combo) return "";
  const names: Record<string, string> = os === "macos"
    ? { ctrl: "Control", alt: "Option", shift: "Shift", super: "Command", cmd: "Command" }
    : { ctrl: "Ctrl", alt: "Alt", shift: "Shift", super: "Win", cmd: "Win" };
  return combo.split("+").map((k) => names[k] ?? k.toUpperCase()).join("+");
}

/** A pressed combination as the engine stores it, or null while only modifiers are down. */
export function comboFrom(e: { ctrlKey: boolean; altKey: boolean; shiftKey: boolean; metaKey: boolean; code: string }): string | null {
  const key = /^Key([A-Z])$/.exec(e.code)?.[1] ?? /^Digit(\d)$/.exec(e.code)?.[1];
  const mods = [e.ctrlKey && "ctrl", e.altKey && "alt", e.shiftKey && "shift", e.metaKey && "super"].filter(Boolean);
  return key && mods.length ? [...mods, key.toLowerCase()].join("+") : null;
}

const SECTIONS = [
  { id: "recording", title: "Recording", meta: "These settings are kept on this computer." },
  { id: "ai", title: "AI engine", meta: "Which program writes the notes, and the models it needs on this computer." },
  { id: "connect", title: "Connect AI tools", meta: "Claude, Cursor and Codex can search, read and add to this vault through a small local server." },
] as const;
type Section = (typeof SECTIONS)[number]["id"];

export function Settings({ api }: { api: Api | null }) {
  const [section, setSection] = useState<Section>("recording");
  const current = SECTIONS.find((x) => x.id === section)!;
  return (
    <div className="settings-frame">
      <nav className="subnav" aria-label="Settings">
        <ul>
          {SECTIONS.map((x) => (
            <li key={x.id}><button className="subnav-item" aria-current={section === x.id ? "page" : undefined}
              onClick={() => setSection(x.id)}>{x.title}</button></li>
          ))}
        </ul>
        <select className="select plain subnav-select" aria-label="Settings section" value={section}
          onChange={(e) => setSection(e.target.value as Section)}>
          {SECTIONS.map((x) => <option key={x.id} value={x.id}>{x.title}</option>)}
        </select>
      </nav>
      <div className="settings">
        <header className="settings-head">
          <h1 className="page-title">{current.title}</h1>
          <p className="head-meta">{current.meta}</p>
        </header>
        {section === "recording" ? <RecordingSettings api={api} /> : section === "ai" ? <AiEngineSettings api={api} />
          : <ConnectSettings api={api} />}
      </div>
    </div>
  );
}

function RecordingSettings({ api }: { api: Api | null }) {
  const [settings, setSettings] = useState<RecordingSettings | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [communications, setCommunications] = useState(false);
  const [os, setOs] = useState<Platform | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    platform().then(setOs);
    micDevices().then((d) => { setDevices(d.devices); setCommunications(d.communications); });
  }, []);
  useEffect(() => {
    if (!api) return;
    const load = () => api.get<RecordingSettings>("/settings/recording").then(setSettings, (e) => setError(String(e.message)));
    load();
    const stream = api.events();
    stream.addEventListener("settings.changed", load);
    return () => stream.close();
  }, [api]);

  const change = async (body: Partial<RecordingSettings>) => {
    if (!api) return;
    try {
      setSettings(await api.patch<RecordingSettings>("/settings/recording", body));
      setError(null);
    } catch (e) {
      setError(`Could not save: ${(e as Error).message}`);
    }
  };

  return (
    <>
      {error && <p className="settings-error tone-failed" role="alert"><WarningCircle size={16} weight="fill" aria-hidden />{error}</p>}
      {!settings ? <div className="skeleton detail-skeleton" aria-label="Loading settings"><div /><div /><div /></div> : (
        <>
          <MicrophoneGroup choice={settings.mic} devices={devices} communications={communications}
            onChoose={(mic) => { micSelect(mic); change({ mic }); }} />

          <section className="sgroup" aria-labelledby="g-auto">
            <div className="group-head">
              <h2 id="g-auto">When a call is detected</h2>
              <span className="muted">This computer only</span>
            </div>
            <label className="choice">
              <input type="radio" name="auto" checked={!settings.auto_record} onChange={() => change({ auto_record: false })} />
              <span>Show the popup; I press Record</span>
            </label>
            <label className="choice">
              <input type="radio" name="auto" checked={settings.auto_record} onChange={() => change({ auto_record: true })} />
              <span>Start recording by itself
                <span className="choice-note">Tell the people you record. The law on this differs by country.</span></span>
            </label>
          </section>

          <section className="sgroup" aria-labelledby="g-popup">
            <div className="group-head"><h2 id="g-popup">The call popup</h2></div>
            <label className={`choice${os && !os.content_protection ? " disabled" : ""}`}>
              <input type="checkbox" checked={settings.hide_popup_from_sharing && !!os?.content_protection}
                disabled={!os?.content_protection} onChange={(e) => change({ hide_popup_from_sharing: e.target.checked })} />
              <span>Hide it from screen sharing where the system allows
                <span className="choice-note">{!os ? "" : !os.content_protection
                  ? "Not available on Linux: the system has no way to keep one window out of a screen share."
                  : os.os === "macos" ? "On macOS 15 and later, apps such as Zoom and Teams can still see it."
                  : "Screen sharing, recordings and screenshots leave it out."}</span></span>
            </label>
          </section>

          <ShortcutsGroup hotkeys={settings.hotkeys} failures={settings.shortcut_failures} os={os}
            onChange={(action, combo) => change({ hotkeys: { [action]: combo } as Hotkeys })} />

          <section className="sgroup" aria-labelledby="g-folder">
            <div className="group-head"><h2 id="g-folder">Where recordings are kept</h2></div>
            <p className="path">{settings.recordings_folder}</p>
            <p className="choice-note">Two files per meeting, you and the call, about 230 MB per hour.</p>
          </section>
        </>
      )}
    </>
  );
}

function MicrophoneGroup({ choice, devices, communications, onChoose }: { choice: MicChoice; devices: Device[];
  communications: boolean; onChoose: (c: MicChoice) => void }) {
  const [meters, setMeters] = useState<[Meter, Meter] | null>(null);
  const [levels, setLevels] = useState<{ mic: number[]; call: number[] }>({ mic: [], call: [] });
  const [check, setCheck] = useState<{ opened: number; heard: number | null; callHeard: boolean }>(() => ({ opened: Date.now(), heard: null, callHeard: false }));
  const [now, setNow] = useState(() => Date.now());
  const value = useRef(choice);

  useEffect(() => {
    micPreview(value.current);
    const timer = window.setInterval(async () => {
      const next = await micLevels().catch(() => null);
      setMeters(next);
      setNow(Date.now());
      if (!next) return;
      setLevels((l) => ({ mic: pushLevel(l.mic, next[0].level, STRIP), call: pushLevel(l.call, next[1].level, STRIP) }));
      setCheck((c) => ({ ...c, heard: c.heard ?? (next[0].level >= HEARD ? Date.now() : null),
        callHeard: c.callHeard || next[1].level >= HEARD }));
    }, 100);
    return () => { window.clearInterval(timer); micPreview(null); };
  }, []);

  const pick = (raw: string) => {
    const dev = devices.find((d) => `device:${d.id}` === raw);
    const next: MicChoice = dev ? { kind: "device", id: dev.id, name: dev.name } : raw === "communications" ? { kind: "communications" } : { kind: "default" };
    value.current = next;
    setCheck({ opened: Date.now(), heard: null, callHeard: check.callHeard });
    setLevels((l) => ({ ...l, mic: [] }));
    onChoose(next);
  };
  const state = micCheck(meters?.[0] ?? null, check.heard, check.opened, now);
  const named = (pred: (d: Device) => boolean) => devices.find(pred)?.name;
  const saved = choice.kind === "device" && !devices.some((d) => d.id === choice.id) ? choice : null;
  const line = {
    listening: { tone: "muted", text: "Say something to test." },
    heard: { tone: "done", text: "We hear you." },
    silent: { tone: "paused", text: "No sound yet. Check that this microphone is not muted, or pick another one." },
    lost: { tone: "failed", text: meters?.[0].status.problem ?? "No microphone found." },
    "fell-back": { tone: "paused", text: `Not connected. Recording ${meters?.[0].status.device ?? "the default microphone"} until it is back.` },
  }[state];

  return (
    <>
      <section className="sgroup" aria-labelledby="g-mic">
        <div className="group-head"><h2 id="g-mic">Microphone</h2></div>
        <div className="select-wrap">
          <Microphone size={16} aria-hidden />
          <select className="select" aria-labelledby="g-mic" value={choiceValue(choice)} onChange={(e) => pick(e.target.value)}>
            <option value="default">System default{named((d) => d.default) ? `: ${named((d) => d.default)}` : ""}</option>
            {communications && <option value="communications">Communications default{named((d) => d.communications) ? `: ${named((d) => d.communications)}` : ""}</option>}
            {devices.map((d) => <option key={d.id} value={`device:${d.id}`}>{d.name}</option>)}
            {saved && <option value={`device:${saved.id}`}>{saved.name} (not connected)</option>}
          </select>
        </div>
        <div className="meter-row">
          <LevelStrip label="You" levels={levels.mic} size={STRIP} />
          <span className={`meter-check tone-${line.tone}`} aria-live="polite">
            {state === "heard" ? <CheckCircle size={14} weight="fill" aria-hidden /> : state !== "listening" ? <WarningCircle size={14} weight="fill" aria-hidden /> : null}
            {line.text}
          </span>
        </div>
      </section>
      <section className="sgroup" aria-labelledby="g-call">
        <div className="group-head"><h2 id="g-call">Call audio</h2><span className="muted">what the others say</span></div>
        <div className="meter-row">
          <LevelStrip label="Call" levels={levels.call} size={STRIP} />
          <span className={`meter-check tone-${check.callHeard ? "done" : "muted"}`}>
            {check.callHeard ? <><CheckCircle size={14} weight="fill" aria-hidden />Sound from {meters?.[1].status.device ?? "the computer"}.</>
              : meters?.[1].status.problem ?? `Play something to test${meters?.[1].status.device ? ` (${meters[1].status.device})` : ""}.`}
          </span>
        </div>
      </section>
    </>
  );
}

function ShortcutsGroup({ hotkeys, failures, os, onChange }: { hotkeys: Hotkeys; failures: Record<string, string>;
  os: Platform | null; onChange: (action: keyof Hotkeys, combo: string) => void }) {
  const [listening, setListening] = useState<keyof Hotkeys | null>(null);
  useEffect(() => {
    if (!listening) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      if (e.key === "Escape") return setListening(null);
      const combo = comboFrom(e);
      if (combo) { onChange(listening, combo); setListening(null); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [listening, onChange]);

  return (
    <section className="sgroup" aria-labelledby="g-keys">
      <div className="group-head"><h2 id="g-keys">Shortcuts</h2><span className="muted">work anywhere on the computer</span></div>
      {os && !os.shortcuts && (
        <p className="choice-note tone-paused">This desktop runs Wayland, where apps cannot register shortcuts that work everywhere.
          Use the tray menu and the popup instead.</p>
      )}
      <table className="keys">
        <tbody>
          {ACTIONS.map(([action, label]) => (
            <tr key={action}>
              <th scope="row">{label}</th>
              <td>{listening === action ? <span className="tone-working">Press the keys, or Esc</span>
                : hotkeys[action] ? <kbd>{keyLabel(hotkeys[action], os?.os ?? "")}</kbd> : <span className="muted">None</span>}
                {failures[action] && <span className="key-failed tone-failed" title={failures[action]}><WarningCircle size={14} weight="fill" aria-hidden />Could not be registered; another app may use it</span>}</td>
              <td className="keys-actions">
                <button className="link-button" onClick={() => setListening(action)} aria-label={`Change: ${label}`}>Change</button>
                {hotkeys[action] && <button className="link-button" onClick={() => onChange(action, "")} aria-label={`Clear: ${label}`}>Clear</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
