// The call popup: a small window at the right edge that never takes the keyboard on its own
// (plan/03-ux-shape.md section 4). It shows what the engine says and does what its buttons say; the
// shell records and keeps the window out of screen sharing.
import {
  ArrowSquareOut, ArrowsClockwise, CaretDown, CheckCircle, Circle, FlagPennant, Heart, Monitor, Plus, Record,
  Scissors, Square, Waveform, WarningCircle, X,
} from "@phosphor-icons/react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { Api, engineInfo } from "./engine";
import { LevelStrip } from "./Level";
import { clock, length, swatches } from "./meetings";
import { Swatch } from "./MeetingsList";
import {
  FOLD_AFTER_SECONDS, CLOSE_AFTER_END_SECONDS, HEARD, STAGE_LINE, clientOf, elapsed, popupView, pushLevel,
  trackProblem, type PopupData,
} from "./recording";
import { onShell, openMain, popupHide, popupLayout, recorderLive, type Live } from "./shell";

const STRIP = 16;
const EVENTS = ["recording.state", "meeting.started", "meeting.updated", "meeting.ended", "pipeline.progress", "settings.changed"];

export function Popup() {
  const [api, setApi] = useState<Api | null>(null);
  const [data, setData] = useState<PopupData | null>(null);
  const [live, setLive] = useState<Live | null>(null);
  const [levels, setLevels] = useState<{ mic: number[]; call: number[] }>({ mic: [], call: [] });
  const [card, setCard] = useState(false);
  const [hover, setHover] = useState(false);
  const [marked, setMarked] = useState<string | null>(null);
  const [colours, setColours] = useState<Map<string, string>>(new Map());
  const [now, setNow] = useState(() => new Date());
  const [problem, setProblem] = useState<string | null>(null);
  const [idleSince, setIdleSince] = useState(() => Date.now());
  const heard = useRef({ mic: 0, call: 0 });
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    engineInfo().then((info) => info.state === "ready" && setApi(new Api(info)));
  }, []);
  const load = useCallback(async () => {
    if (!api) return;
    const [popup, clients] = await Promise.all([api.get<PopupData>("/popup"),
      api.get<{ clients: { name: string }[] }>("/clients")]);
    setData(popup);
    setColours(swatches(clients.clients.map((c) => c.name)));
  }, [api]);
  useEffect(() => {
    if (!api) return;
    const stream = api.events();
    const reload = () => { load().catch(() => {}); };
    stream.onopen = reload;
    for (const name of EVENTS) stream.addEventListener(name, reload);
    const offShow = onShell<{ card?: boolean }>("popup-show", (p) => {
      reload();
      setIdleSince(Date.now());                // shown again: the buttons stay for another ten seconds
      if (p?.card) setCard(true);
    });
    const offRec = onShell("recorder", reload);
    return () => { stream.close(); offShow(); offRec(); };
  }, [api, load]);

  const view = popupView(data, now);
  const recording = view.kind === "recording";
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  // the meters are data: they move ten times a second while recording
  useEffect(() => {
    if (!recording) return;
    heard.current = { mic: Date.now(), call: Date.now() };
    const timer = window.setInterval(async () => {
      const next = await recorderLive().catch(() => null);
      setLive(next);
      if (!next) return;
      const t = Date.now();
      if (next.mic_level >= HEARD) heard.current.mic = t;
      if (next.system_level >= HEARD) heard.current.call = t;
      setProblem(trackProblem(next, (t - heard.current.call) / 1000, (t - heard.current.mic) / 1000));
      setLevels((l) => ({ mic: pushLevel(l.mic, next.mic_level, STRIP), call: pushLevel(l.call, next.system_level, STRIP) }));
    }, 100);
    return () => window.clearInterval(timer);
  }, [recording]);

  // fold to the top line after 10 s without the pointer; close 20 s after the call ended, unless hovered
  const folded = recording && !card && !hover && (now.getTime() - idleSince) / 1000 >= FOLD_AFTER_SECONDS;
  const endedFor = data?.session?.end ? (now.getTime() - new Date(data.session.end).getTime()) / 1000 : 0;
  const closeNow = view.kind === "ended" && !data?.live && !hover && !card && endedFor >= CLOSE_AFTER_END_SECONDS;
  useEffect(() => { if (closeNow) popupHide(); }, [closeNow]);

  // the window is exactly as tall as what it shows; only the picker card may take the keyboard
  useLayoutEffect(() => {
    const el = root.current;
    if (!el) return;
    const observer = new ResizeObserver(() => popupLayout(Math.ceil(el.getBoundingClientRect().height), card));
    observer.observe(el);
    return () => observer.disconnect();
  }, [card]);

  const act = (run: () => Promise<unknown>) => { run().then(load, () => load()); };
  const session = data?.session ?? null;
  const rec = data?.recording ?? null;
  const record = () => act(() => api!.post(session && data?.live ? `/sessions/${session.id}/record` : "/recording/start"));
  const mark = () => {
    if (!rec) return;
    const at = clock(elapsed(rec, live, new Date()));
    act(() => api!.post("/recording/mark"));
    setMarked(at);
    window.setTimeout(() => setMarked(null), 2000);
  };
  const shot = (kind: "region" | "screen") => session && act(() => api!.post(`/sessions/${session.id}/screenshot`, { kind }));
  const hide = () => { setCard(false); popupHide(); };
  const client = data ? clientOf(data) : null;
  const chip = (
    <button className="chip-client" onClick={() => setCard(true)} disabled={!session}
      aria-label={client ? `Client: ${client}. Change` : "Choose the client"}>
      <Swatch client={client} colours={colours} />
      <span className="chip-name">{client ?? "Client?"}</span>
      <CaretDown size={12} aria-hidden />
    </button>
  );
  const close = <button className="icon-button" onClick={hide} aria-label="Hide the popup (nothing is decided)" title="Hide"><X size={16} aria-hidden /></button>;
  const callQuiet = problem === "Call: no sound?";
  const trouble = callQuiet ? null : problem;

  let line1, line2;
  if (view.kind === "recording" && rec) {
    line1 = (
      <>
        <span className="rec-dot" aria-hidden><Circle size={10} weight="fill" /></span>
        <span className="rec-time num" aria-label={`Recording, ${clock(elapsed(rec, live, now))}`}>{clock(elapsed(rec, live, now))}</span>
        {trouble
          ? <span className="bar-problem tone-paused" role="status"><WarningCircle size={14} weight="fill" aria-hidden />{trouble}</span>
          : <>
            <LevelStrip label="You" levels={levels.mic} size={STRIP} />
            <LevelStrip label={callQuiet ? "Call: no sound?" : "Call"} levels={levels.call} size={STRIP} warn={callQuiet} />
          </>}
        <span className="bar-end">{view.ask ? close : chip}</span>
      </>
    );
    line2 = view.ask ? (
      <>
        <span className="bar-ask">Who is this with?</span>
        {chip}
        <button className="link-button" onClick={() => setCard(true)}>Title (optional)</button>
      </>
    ) : (
      <>
        <button className="pbutton" onClick={() => act(() => api!.post("/recording/stop"))}><Square size={14} weight="fill" aria-hidden />Stop</button>
        <span className="split" role="group" aria-label="Screenshot">
          <button className="pbutton" onClick={() => shot("region")} disabled={!session} title="Snip a region; it is attached to this meeting">
            <Scissors size={14} aria-hidden />Snip</button>
          <button className="pbutton" onClick={() => shot("screen")} disabled={!session}
            title="Whole screen (who is in the meeting)" aria-label="Whole screen (who is in the meeting)">
            <Monitor size={14} aria-hidden /></button>
        </span>
        <button className="pbutton" onClick={mark} aria-live="polite"><FlagPennant size={14} aria-hidden />{marked ? <>Marked <span className="num">{marked}</span></> : "Mark"}</button>
        <span className="bar-end">{close}</span>
      </>
    );
  } else if (view.kind === "ended" && rec) {
    const stage = data?.stage ?? "saved";
    line1 = (
      <>
        <span className="bar-text">
          {["Saved", rec.seconds < 60 ? `${rec.seconds} s` : length(rec.seconds)].join(" · ")}{" · "}
          <span className="inline-client"><Swatch client={client} colours={colours} />{client ?? "Client?"}</span>
          {session?.call_title ? ` · ${session.call_title}` : ""}
        </span>
        <span className="bar-end">{close}</span>
      </>
    );
    line2 = (
      <>
        <span className={`stage ${stage === "ready" ? "tone-done" : stage === "review" || stage === "failed" ? "tone-failed" : "tone-working"}`}>
          {stage === "ready" ? <CheckCircle size={14} weight="fill" aria-hidden /> : stage === "review" || stage === "failed"
            ? <WarningCircle size={14} weight="fill" aria-hidden /> : <Waveform size={14} weight="bold" aria-hidden />}
          {STAGE_LINE[stage]}
        </span>
        <span className="bar-end">
          {view.recordAgain && <button className="pbutton" onClick={record}><ArrowsClockwise size={14} aria-hidden />Record again</button>}
          <button className="pbutton" onClick={() => openMain(session?.id ?? null)}><ArrowSquareOut size={14} aria-hidden />Open meeting</button>
        </span>
      </>
    );
  } else if (view.kind === "failed") {
    line1 = (
      <>
        <span className="bar-problem tone-failed" role="alert"><WarningCircle size={14} weight="fill" aria-hidden />Could not record: {view.message}</span>
        <span className="bar-end">{close}</span>
      </>
    );
    line2 = <button className="record-button" onClick={record}><Record size={14} weight="fill" aria-hidden />Try again</button>;
  } else if (view.kind === "detected" && session) {
    line1 = (
      <>
        <span className="bar-text" title={data?.headline}>{data?.headline}</span>
        <span className="bar-end">{close}</span>
      </>
    );
    line2 = (
      <>
        <button className="record-button" onClick={record}><Record size={14} weight="fill" aria-hidden />Record</button>
        {chip}
        <span className="bar-end">
          <button className="link-button" title="Nothing from this meeting is processed"
            onClick={() => { api!.patch(`/sessions/${session.id}`, { skip: true }).then(hide, load); }}>Not this one</button>
        </span>
      </>
    );
  } else {
    line1 = (
      <>
        <span className="bar-text muted">No call right now</span>
        <span className="bar-end">{close}</span>
      </>
    );
    line2 = <button className="record-button" onClick={record} disabled={!api}><Record size={14} weight="fill" aria-hidden />Record</button>;
  }

  const showLine2 = !folded;
  return (
    <div ref={root} className={`popup${card ? " popup-card" : ""}`} aria-label="Call"
      onPointerEnter={() => setHover(true)}
      onPointerLeave={() => { setHover(false); setIdleSince(Date.now()); }}
      onKeyDown={(e: KeyboardEvent) => { if (e.key === "Escape" && !card) hide(); }}>
      <div className="bar-line" data-tauri-drag-region>{line1}</div>
      {card && session && api
        ? <PickerCard api={api} session={session.id} colours={colours} onDone={() => { setCard(false); load(); }} />
        : showLine2 && <div className="bar-line">{line2}</div>}
    </div>
  );
}

type PickerRow = { kind: "client" | "create" | "personal" | "unsorted" | "skip"; label: string; client: string | null; via: string | null };
type PickerView = { rows: PickerRow[]; selected: number; mode: string; prompt: string; hint: string; field_value: string;
  query: string; title: string; headline: string; done?: boolean };

/** The client picker (picker.py, driven over HTTP). The one state that holds the keyboard, because the user opened it. */
function PickerCard({ api, session, colours, onDone }: { api: Api; session: string; colours: Map<string, string>; onDone: () => void }) {
  const [view, setView] = useState<PickerView | null>(null);
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const seq = useRef(0);
  const input = useRef<HTMLInputElement>(null);

  const apply = (next: PickerView, typed?: string) => {
    if (next.done) return onDone();
    setView((old) => {
      if (!old || old.mode !== next.mode) setText(typed ?? next.field_value ?? "");
      return next;
    });
  };
  useEffect(() => {
    api.post<PickerView>("/picker/open", { session }).then((v) => { setTitle(v.title); apply(v); }, onDone);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, session]);
  useEffect(() => { input.current?.focus(); }, [view?.mode]);

  const send = (type: string, value?: unknown) => {
    const mine = ++seq.current;
    return api.post<PickerView>("/picker/event", { session, type, value }).then((v) => {
      if (mine === seq.current || v.done) apply(v, type === "query" ? String(value) : undefined);    // typing fast: only the newest answer
    });
  };
  const browse = view?.mode === "browse";
  const enter = () => {
    if (!view) return;
    if (view.mode === "duplicate") send("answer_duplicate", true);
    else if (browse) send("confirm");
    else send("submit_field", text);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); send("move", e.key === "ArrowDown" ? 1 : -1); }
    else if (e.key === "Enter") { e.preventDefault(); enter(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); if (browse) send("cancel"); else send("back"); }
  };
  const rowLabel = (row: PickerRow) => {
    const q = view?.query.trim();
    if (row.kind === "create") return <><Plus size={14} aria-hidden />{q ? `New client: ${q}` : "New client…"}</>;
    if (row.kind === "personal") return <><Heart size={14} aria-hidden />{q ? `Personal call: ${q}` : "Personal call (not a client)…"}</>;
    if (row.kind === "client") {
      const via = row.via && row.via !== "name" ? row.label.slice(row.label.indexOf("(")) : "";
      return <><Swatch client={row.client} colours={colours} />{row.client}{via && <span className="muted">{via}</span>}</>;
    }
    return <>{row.label}</>;
  };

  if (!view) return <div className="card" aria-busy="true" />;
  return (
    <div className="card">
      <h2 className="card-title" id="picker-title">{browse ? "Who is this call with?" : view.prompt}</h2>
      <input ref={input} className="input" value={text} aria-labelledby="picker-title" autoComplete="off" spellCheck={false}
        role={browse ? "combobox" : undefined} aria-expanded={browse ? true : undefined} aria-controls={browse ? "picker-rows" : undefined}
        aria-activedescendant={browse ? `picker-row-${view.selected}` : undefined}
        onChange={(e) => { setText(e.target.value); if (browse) send("query", e.target.value); }} onKeyDown={onKey}
        placeholder={browse ? "Type a client, alias or contact" : ""} />
      {view.mode === "duplicate" ? (
        <div className="card-choice">
          <button className="pbutton" onClick={() => send("answer_duplicate", true)}>Yes, that one</button>
          <button className="pbutton" onClick={() => send("answer_duplicate", false)}>No, a new client</button>
        </div>
      ) : browse ? (
        <ul className="picker-rows" id="picker-rows" role="listbox" aria-label="Clients">
          {view.rows.map((row, i) => (
            <li key={`${row.kind}${row.label}`} id={`picker-row-${i}`} role="option" aria-selected={i === view.selected}
              className={`picker-row kind-${row.kind}`} onPointerDown={(e) => e.preventDefault()}
              onClick={() => send("select", i).then(() => send("confirm"))}>
              {rowLabel(row)}
            </li>
          ))}
        </ul>
      ) : <p className="card-note">{view.mode === "contact" || view.mode === "company" || view.mode === "note" ? "Enter skips this." : ""}</p>}
      {browse && (
        <label className="card-field">
          <span>Title</span>
          <input className="input" value={title} placeholder="Optional" onChange={(e) => { setTitle(e.target.value); send("title", e.target.value); }}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); enter(); } if (e.key === "Escape") { e.preventDefault(); send("cancel"); } }} />
        </label>
      )}
      <p className="card-hint">{view.hint ? `${view.hint} · ` : ""}{browse ? "Up/Down choose · Enter confirm · Esc later" : "Enter next · Esc back to the list"}</p>
    </div>
  );
}

