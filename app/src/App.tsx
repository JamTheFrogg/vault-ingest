import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { downloadStatus, type Progress } from "./ai";
import { ClientPage, ClientsList, type ClientRow } from "./Clients";
import { Api, engineInfo, restartEngine, type EngineInfo } from "./engine";
import { EmptyScreen } from "./EmptyScreen";
import { GraphView } from "./GraphView";
import { MeetingDetail } from "./MeetingDetail";
import { MeetingsList } from "./MeetingsList";
import { buildRows, swatches, type MeetingsData, type Status } from "./meetings";
import { crumbs, splitTarget } from "./note";
import { Palette } from "./Palette";
import type { Command, Hit, Item } from "./palette";
import { NoteReader, obsidianUrl, openExternal } from "./Reader";
import { Rail, type View } from "./Rail";
import type { Recording } from "./recording";
import { Settings } from "./Settings";
import { onShell } from "./shell";

export type Conn = { state: "starting" } | { state: "ready"; api: Api } | { state: "stopped"; error: string };
/** Where the window is: a section, the client open there, and a note opened over it (with its #heading). */
export type Location = { view: View; client?: string | null; note?: string | null };
const EVENTS = ["meeting.started", "meeting.updated", "meeting.ended", "pipeline.progress", "gpu.state", "recording.state"];
const VIEWS: View[] = ["meetings", "clients", "graph", "import"];

function toConn(info: EngineInfo): Conn {
  return info.state === "ready" ? { state: "ready", api: new Api(info) } : info;
}

export function App() {
  const [conn, setConn] = useState<Conn>({ state: "starting" });
  const [meetings, setMeetings] = useState<MeetingsData & { recording?: Recording | null } | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [clients, setClients] = useState<ClientRow[] | null>(null);
  const [history, setHistory] = useState<{ list: Location[]; at: number }>({ list: [{ view: "meetings" }], at: 0 });
  const [selected, setSelected] = useState<string | null>(null);
  const [now, setNow] = useState(() => new Date());
  const [downloads, setDownloads] = useState<Record<string, Progress>>({});
  const [versions, setVersions] = useState<Record<string, number>>({});      // path -> when it last changed on disk
  const [palette, setPalette] = useState<string | null>(null);              // the text it opens with; null: closed
  const [recent, setRecent] = useState<Hit[]>([]);
  const reload = useRef<number | undefined>(undefined);
  const loc = history.list[history.at];
  const view = loc.view;

  useEffect(() => {
    engineInfo().then((info) => setConn(toConn(info)));
  }, []);

  const api = conn.state === "ready" ? conn.api : null;
  const load = useCallback(async () => {
    if (!api) return;
    const [m, s, c] = await Promise.all([
      api.get<MeetingsData>("/meetings"), api.get<Status>("/status"), api.get<{ clients: ClientRow[] }>("/clients"),
    ]);
    setMeetings(m);
    setStatus(s);
    setClients(c.clients);
    setNow(new Date());
  }, [api]);

  useEffect(() => {
    if (!api) return;
    let gone = false;
    const lost = async () => {
      // the stream retries by itself; ask whether the engine is still there before saying it is not
      try {
        await api.get("/status");
      } catch {
        if (gone) return;
        const info = await engineInfo().catch(() => null);
        setConn(info?.state === "stopped" ? info
          : { state: "stopped", error: `The engine at ${api.base} does not answer.` });
      }
    };
    const stream = api.events();
    stream.onopen = () => load().catch(lost);          // also after the stream reconnects: nothing missed stays stale
    const soon = () => {
      window.clearTimeout(reload.current);
      reload.current = window.setTimeout(() => load().catch(lost), 250);
    };
    for (const name of EVENTS) stream.addEventListener(name, soon);
    stream.addEventListener("notes.changed", (e) => {
      const { paths } = JSON.parse((e as MessageEvent).data) as { paths: string[] };
      const at = Date.now();
      setVersions((v) => ({ ...v, ...Object.fromEntries(paths.map((p) => [p, at])) }));
      soon();
    });
    stream.addEventListener("download.progress", (e) => {
      const p = JSON.parse((e as MessageEvent).data) as Progress;
      setDownloads((d) => ({ ...d, [p.id]: p }));
    });
    stream.onerror = lost;
    return () => {
      gone = true;
      stream.close();
      window.clearTimeout(reload.current);
    };
  }, [api, load]);

  const rows = useMemo(() => (meetings ? buildRows(meetings, status, now) : null), [meetings, status, now]);
  const names = useMemo(() => (clients ?? []).map((c) => c.name), [clients]);
  const colours = useMemo(() => swatches([...names, ...(rows ?? []).map((r) => r.client)]), [names, rows]);
  const hasLive = rows?.some((r) => r.stage === "live" || r.stage === "recording") ?? false;
  useEffect(() => {
    if (!hasLive) return;
    const timer = window.setInterval(() => setNow(new Date()), 1000);        // the live call's timer
    return () => window.clearInterval(timer);
  }, [hasLive]);

  // ------------------------------------------------------------ navigation: history like a browser
  const navigate = useCallback((next: Location) => {
    setHistory((h) => {
      const here = h.list[h.at];
      if (here.view === next.view && (here.client ?? null) === (next.client ?? null) && (here.note ?? null) === (next.note ?? null)) return h;
      return { list: [...h.list.slice(0, h.at + 1), next].slice(-100), at: Math.min(h.at + 1, 99) };
    });
  }, []);
  const go = useCallback((delta: number) => setHistory((h) => ({ ...h, at: Math.max(0, Math.min(h.list.length - 1, h.at + delta)) })), []);
  const setView = useCallback((v: View) => navigate({ view: v, client: v === "clients" ? loc.client : null }), [navigate, loc.client]);
  const openNote = useCallback((target: string) => {
    navigate({ view: view === "settings" || view === "import" ? "meetings" : view, client: loc.client, note: target });
    const path = splitTarget(target).path;
    setRecent((r) => [{ path, title: crumbs(path).name, type: "", client: null, date: null, snippet: [] },
      ...r.filter((x) => x.path !== path)].slice(0, 5));
  }, [navigate, view, loc.client]);
  const openMeeting = useCallback((path: string) => {
    const key = `call:${path}`;
    if (rows?.some((r) => r.key === key)) {
      setSelected(key);
      navigate({ view: "meetings" });
    } else openNote(path);
  }, [rows, navigate, openNote]);
  const selectMeeting = (key: string) => {
    setSelected(key);
    if (loc.note) navigate({ view: "meetings" });
  };

  const restart = async () => {
    setConn({ state: "starting" });
    setConn(toConn(await restartEngine()));
  };
  const row = rows?.find((r) => r.key === selected) ?? null;
  const act = (path: string) => { api?.post(path).then(load, load); };
  const latest = useRef(rows);
  useEffect(() => { latest.current = rows; }, [rows]);
  useEffect(() => onShell<string>("open-meeting", (sid) => {
    // the popup's "Open meeting": the note when there is one, else the recording or the live call
    const found = latest.current?.find((r) => r.session?.id === sid && r.stage === "ready") ?? latest.current?.find((r) => r.session?.id === sid);
    navigate({ view: "meetings" });
    if (found) setSelected(found.key);
  }), [navigate]);

  // the note on screen, for Open in Obsidian and Copy link
  const current = loc.note ? splitTarget(loc.note).path
    : view === "meetings" && row?.call ? row.call.call_note
    : view === "clients" ? clients?.find((c) => c.name === loc.client)?.profile ?? null : null;
  const vault = status?.vault ?? "";
  const recording = meetings?.recording;
  const live = recording && (recording.state === "starting" || recording.state === "recording");
  const commands: (Command & { run: () => void })[] = [
    ...(live ? [{ id: "stop", label: "Stop recording", run: () => act("/recording/stop") },
      { id: "mark", label: "Mark this moment", run: () => act("/recording/mark") }]
      : [{ id: "record", label: "Start recording", run: () => act("/recording/start") }]),
    ...(current && vault ? [{ id: "obsidian", label: "Open this note in Obsidian", hint: "Ctrl+O", run: () => openExternal(obsidianUrl(vault, current)) },
      { id: "copy", label: "Copy link to this note", hint: "Ctrl+L", run: () => { navigator.clipboard?.writeText(`[[${current.replace(/\.md$/, "")}]]`).catch(() => {}); } }] : []),
    ...VIEWS.map((v, i) => ({ id: `go-${v}`, label: `Go to ${v[0].toUpperCase()}${v.slice(1)}`, hint: `Ctrl+${i + 1}`, run: () => setView(v) })),
    { id: "go-settings", label: "Go to Settings", hint: "Ctrl+,", run: () => setView("settings") },
  ];
  const pick = (item: Item, inObsidian: boolean) => {
    setPalette(null);
    if (item.kind === "command") commands.find((c) => c.id === item.id)?.run();
    else if (item.kind === "client") navigate({ view: "clients", client: item.name });
    else if (inObsidian && vault) openExternal(obsidianUrl(vault, item.hit.path));
    else if (item.hit.type === "client" && item.hit.client) navigate({ view: "clients", client: item.hit.client });
    else if (item.hit.type === "call") openMeeting(item.hit.path);
    else openNote(item.hit.path);
  };

  const keys = useRef<(e: KeyboardEvent) => void>(() => {});
  useEffect(() => {
    keys.current = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((p) => (p === null ? "" : null)); return; }
      if (palette !== null) return;
      if (mod && /^[1-4]$/.test(e.key)) { e.preventDefault(); setView(VIEWS[Number(e.key) - 1]); }
      else if (mod && e.key === ",") { e.preventDefault(); setView("settings"); }
      else if ((e.altKey && e.key === "ArrowLeft") || (e.metaKey && e.key === "[")) { e.preventDefault(); go(-1); }
      else if ((e.altKey && e.key === "ArrowRight") || (e.metaKey && e.key === "]")) { e.preventDefault(); go(1); }
      else if (mod && e.key.toLowerCase() === "o" && current && vault) { e.preventDefault(); openExternal(obsidianUrl(vault, current)); }
      else if (mod && e.key.toLowerCase() === "l" && current) { e.preventDefault(); commands.find((c) => c.id === "copy")?.run(); }
    };
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keys.current(e);
    const onMouse = (e: MouseEvent) => { if (e.button === 3 || e.button === 4) { e.preventDefault(); go(e.button === 3 ? -1 : 1); } };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mouseup", onMouse);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("mouseup", onMouse); };
  }, [go]);

  const reader = (backLabel: string) => loc.note && (
    <NoteReader key={loc.note} api={api} target={loc.note} vault={vault} version={versions[splitTarget(loc.note).path] ?? 0}
      onOpen={openNote} onBack={() => go(-1)} backLabel={backLabel} onSearch={(t) => setPalette(t)} />
  );
  const client = loc.client ? clients?.find((c) => c.name === loc.client) ?? (clients ? null : undefined) : undefined;

  return (
    <div className="app">
      <Rail view={view} onView={setView} conn={conn} status={status} rows={rows ?? []} recording={meetings?.recording ?? null}
        now={now} onRecord={() => act("/recording/start")} onStop={() => act("/recording/stop")} onSearch={() => setPalette("")}
        download={downloadStatus(Object.values(downloads))} />
      <main className="content" aria-label={view}>
        {conn.state === "stopped" && (
          <div className="banner" role="alert">
            <span><strong>The engine stopped.</strong> {conn.error}</span>
            <button className="button" onClick={restart}>Restart engine</button>
          </div>
        )}
        {view === "meetings" ? (
          <div className="meetings">
            <MeetingsList rows={rows} conn={conn} now={now} colours={colours} selected={selected} onSelect={selectMeeting}
              onStop={() => act("/recording/stop")} />
            <MeetingDetail row={row} api={api} status={status} colours={colours} now={now} note={loc.note ?? null}
              version={loc.note ? versions[splitTarget(loc.note).path] ?? 0 : 0} onOpen={openNote} onBack={() => go(-1)}
              onSearch={(t) => setPalette(t)} onStop={() => act("/recording/stop")} onMark={() => act("/recording/mark")} />
          </div>
        ) : view === "clients" ? (
          <div className="meetings">
            <ClientsList rows={clients} colours={colours} selected={loc.client ?? null}
              onSelect={(name) => navigate({ view: "clients", client: name })} />
            {loc.note ? reader(loc.client ? `Back to ${loc.client}` : "Back")
              : <ClientPage api={api} client={client} name={loc.client ?? null} vault={vault} colours={colours}
                version={client?.profile ? versions[client.profile] ?? 0 : 0} onOpen={openNote} onMeeting={openMeeting} />}
          </div>
        ) : view === "graph" ? (
          <>
            <div className="graph-host" hidden={!!loc.note}>
              <GraphView api={api} vault={vault} colours={colours} clients={names} version={Math.max(0, ...Object.values(versions))}
                onOpen={openNote} onImport={() => setView("import")} />
            </div>
            {reader("Back to graph")}
          </>
        ) : view === "settings" ? <Settings api={api} /> : loc.note ? reader("Back") : <EmptyScreen view={view} />}
      </main>
      {palette !== null && (
        <Palette api={api} initial={palette} clients={clients ?? []} commands={commands} recent={recent}
          onPick={pick} onClose={() => setPalette(null)} />
      )}
    </div>
  );
}
