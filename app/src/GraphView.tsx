// Graph: every note of the vault as a node, every resolved [[link]] as a line (UX plan 3.3, Oxide look).
import { ArrowLeft, ArrowSquareOut, ArrowsIn, FileText, Funnel, Graph as GraphIcon, ListBullets, Minus, Plus, X } from "@phosphor-icons/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods, type LinkObject, type NodeObject } from "react-force-graph-2d";
import type { Api } from "./engine";
import { DEFAULT_SHOW, TYPE_LABEL, graphQuery, local, neighbours, radius, step, visible, type GraphData, type GraphNode, type Show } from "./graph";
import { section } from "./note";
import { fold } from "./palette";
import { openExternal, obsidianUrl, useNote } from "./Reader";

type Node = NodeObject<GraphNode>;
type Edge = LinkObject<GraphNode, object>;
type Props = { api: Api | null; vault: string; colours: Map<string, string>; clients: string[]; version: number;
  onOpen: (path: string) => void; onImport: () => void };

// canvas cannot read CSS variables: the Oxide values (styles.css :root, DESIGN-direction.md 3)
const C = { ground: "#181513", text: "#efe8dc", muted: "#968c80", line: "#4a413a", neutral: "#97a3aa" };
const FONT = '"Atkinson Hyperlegible Next", system-ui, "Segoe UI", sans-serif';
const DIM = 0.25;
const MAX_FIT = 2.5;
const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const idOf = (end: Edge["source"]) => (typeof end === "object" ? (end as Node).id : end) as string;

function loadPositions(vault: string): Record<string, [number, number]> {
  try {
    return JSON.parse(localStorage.getItem(`graph.positions:${vault}`) ?? "{}");
  } catch {
    return {};
  }
}

export function GraphView({ api, vault, colours, clients, version, onOpen, onImport }: Props) {
  const [show, setShow] = useState<Show>(DEFAULT_SHOW);
  const [client, setClient] = useState("");
  const [result, setResult] = useState<{ key: string; data: GraphData } | { key: string; error: string } | null>(null);
  const [find, setFind] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [focusOn, setFocusOn] = useState<{ id: string; depth: number } | null>(null);
  const [asList, setAsList] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const box = useRef<HTMLDivElement>(null);
  const fg = useRef<ForceGraphMethods<Node, Edge> | undefined>(undefined);
  const lastClick = useRef<{ id: string; at: number } | null>(null);
  const fitted = useRef<object | null>(null);           // the data the view was last fitted to

  const query = graphQuery(show, client);
  const key = `${JSON.stringify(query)}|${version}`;
  useEffect(() => {
    if (!api) return;
    let live = true;
    performance.clearMarks("graph-data");
    api.get<GraphData>("/graph", query).then(
      (data) => { if (live) { performance.mark("graph-data"); setResult({ key, data }); } },
      (e) => live && setResult({ key, error: String(e.message ?? e) }));
    return () => { live = false; };
    // the query object is new each render; its key is what changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, key]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(el);
    return () => observer.disconnect();
  }, [asList]);

  const data = result && "data" in result ? result.data : null;
  const shown = useMemo(() => {
    if (!data) return null;
    const base = visible(data, show);
    return focusOn && base.nodes.some((n) => n.id === focusOn.id) ? local(base, focusOn.id, focusOn.depth) : base;
  }, [data, show, focusOn]);
  const near = useMemo(() => neighbours(shown?.edges ?? []), [shown]);
  const graphData = useMemo(() => {
    if (!shown) return { nodes: [] as Node[], links: [] as Edge[] };
    const saved = loadPositions(vault);
    const nodes: Node[] = shown.nodes.map((n) => ({ ...n, ...(saved[n.id] ? { x: saved[n.id][0], y: saved[n.id][1] } : {}) }));
    return { nodes, links: shown.edges.map((e) => ({ ...e })) };
  }, [shown, vault]);
  const allPlaced = graphData.nodes.length > 0 && graphData.nodes.every((n) => n.x !== undefined);

  const matches = useMemo(() => {
    const q = fold(find.trim());
    return q ? new Set(graphData.nodes.filter((n) => fold(n.title).includes(q)).map((n) => n.id)) : null;
  }, [find, graphData]);
  const lit = useMemo(() => {
    const at = hover ?? null;
    if (at) return new Set([at, ...(near.get(at)?.out ?? []), ...(near.get(at)?.in ?? [])]);
    return matches;
  }, [hover, near, matches]);

  const select = useCallback((id: string | null, centre = false) => {
    setSelected(id);
    if (id && centre) {
      const n = graphData.nodes.find((x) => x.id === id);
      if (n?.x !== undefined) fg.current?.centerAt(n.x, n.y, reduced() ? 0 : 300);
    }
  }, [graphData]);

  const fit = (ms = reduced() ? 0 : 300) => {
    fg.current?.zoomToFit(ms, 60);
    // a small graph would fill the screen with a few huge circles: stop at 250%
    window.setTimeout(() => { if ((fg.current?.zoom() ?? 0) > MAX_FIT) fg.current?.zoom(MAX_FIT, 0); }, ms + 20);
  };
  const zoomBy = (f: number) => fg.current?.zoom((fg.current?.zoom() ?? 1) * f, reduced() ? 0 : 150);
  const settled = () => {
    if (performance.getEntriesByName("graph-data").length) performance.measure("graph-settled", "graph-data");
    const saved = loadPositions(vault);
    for (const n of graphData.nodes) if (n.x !== undefined && n.y !== undefined) saved[n.id] = [Math.round(n.x), Math.round(n.y)];
    try {
      localStorage.setItem(`graph.positions:${vault}`, JSON.stringify(saved));
    } catch { /* storage full: the layout is simply computed again next time */ }
    if (fitted.current !== graphData) {
      fitted.current = graphData;
      fit(0);
    }
  };

  const onKey = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).closest("input, select")) return;
    const dirs: Record<string, "left" | "right" | "up" | "down"> = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };
    if (e.key === "+" || e.key === "=") zoomBy(1.4);
    else if (e.key === "-") zoomBy(1 / 1.4);
    else if (e.key === "0") fit();
    else if (e.key === "Escape") { setSelected(null); setFocusOn(null); }
    else if (e.key === "Enter" && selected && !selected.startsWith("missing:")) onOpen(selected);
    else if (dirs[e.key]) {
      const nodes = graphData.nodes;
      const from = nodes.find((n) => n.id === selected) ?? nodes.find((n) => n.type === "client") ?? nodes[0];
      if (!from) return;
      if (!selected) { select(from.id, true); e.preventDefault(); return; }
      const ids = new Set([...(near.get(from.id)?.out ?? []), ...(near.get(from.id)?.in ?? [])]);
      const next = step({ x: from.x ?? 0, y: from.y ?? 0 },
        nodes.filter((n) => ids.has(n.id)).map((n) => ({ id: n.id, x: n.x ?? 0, y: n.y ?? 0 })), dirs[e.key]);
      if (next) select(next, true);
    } else return;
    e.preventDefault();
  };

  const paint = (node: Node, ctx: CanvasRenderingContext2D, scale: number) => {
    const r = radius(node);
    const x = node.x ?? 0;
    const y = node.y ?? 0;
    const colour = node.client ? colours.get(node.client) ?? C.neutral : C.neutral;
    ctx.globalAlpha = lit && !lit.has(node.id) ? DIM : 1;
    ctx.beginPath();
    if (node.type === "other") {
      ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath();
      ctx.fillStyle = C.muted;
      ctx.fill();
    } else if (node.type === "missing") {
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.setLineDash([2 / scale, 2 / scale]);
      ctx.strokeStyle = C.muted;
      ctx.lineWidth = 1 / scale;
      ctx.stroke();
      ctx.setLineDash([]);
    } else {
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = node.type === "transcript-chunk" ? C.muted : colour;
      ctx.fill();
      if (node.type === "client") {
        ctx.strokeStyle = C.text;
        ctx.lineWidth = 1 / scale;
        ctx.stroke();
      }
    }
    if (node.id === selected) {
      ctx.beginPath();
      ctx.arc(x, y, r + 3 / scale, 0, 2 * Math.PI);
      ctx.strokeStyle = C.text;
      ctx.lineWidth = 2 / scale;
      ctx.stroke();
    }
    const labelled = node.type === "client" || node.id === selected || node.id === hover || (matches?.has(node.id) ?? false)
      || (scale > 2.2 && node.type !== "transcript-chunk");
    if (labelled) {
      const px = (node.type === "client" ? 12 : 11) / scale;
      ctx.font = `${node.type === "client" ? 500 : 400} ${px}px ${FONT}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const label = node.title.length > 40 ? `${node.title.slice(0, 39)}…` : node.title;
      ctx.lineWidth = 3 / scale;
      ctx.strokeStyle = C.ground;
      ctx.strokeText(label, x, y + r + 3 / scale);
      ctx.fillStyle = node.type === "client" || node.id === selected || node.id === hover ? C.text : C.muted;
      ctx.fillText(label, x, y + r + 3 / scale);
    }
    ctx.globalAlpha = 1;
  };
  const hitArea = (node: Node, colour: string, ctx: CanvasRenderingContext2D) => {
    ctx.fillStyle = colour;
    ctx.beginPath();
    ctx.arc(node.x ?? 0, node.y ?? 0, Math.max(radius(node), 4) + 1, 0, 2 * Math.PI);
    ctx.fill();
  };
  const linkColour = (l: Edge) => {
    const s = idOf(l.source);
    const t = idOf(l.target);
    if (hover) return s === hover || t === hover ? C.text : "rgba(74, 65, 58, 0.25)";
    if (selected && (s === selected || t === selected)) return C.text;
    if (lit) return lit.has(s) && lit.has(t) ? C.line : "rgba(74, 65, 58, 0.25)";
    return C.line;
  };

  const clicked = (node: Node) => {
    const now = Date.now();
    if (lastClick.current?.id === node.id && now - lastClick.current.at < 400 && node.type !== "missing") onOpen(node.id);
    lastClick.current = { id: node.id, at: now };
    select(node.id);
  };

  const total = data?.nodes.length ?? 0;
  const filtered = JSON.stringify(show) !== JSON.stringify(DEFAULT_SHOW) || client !== "";
  const resetFilters = () => { setShow(DEFAULT_SHOW); setClient(""); setFocusOn(null); };
  const sel = graphData.nodes.find((n) => n.id === selected) ?? null;
  // one line per pair of notes, however many times and in which direction they link
  const pairs = new Set((shown?.edges ?? []).map((e) => [e.source, e.target].sort().join("\n"))).size;
  const bare = !!data && total === 0 && !filtered;           // an empty vault: nothing to filter or zoom

  return (
    <section className="graph-screen" aria-label="Graph">
      <header className="graph-bar">
        <h1 className="list-title">Graph</h1>
        {!bare && <input className="input graph-find" placeholder="Find a note" value={find} aria-label="Find a note in the graph"
          onChange={(e) => setFind(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && matches?.size) select([...matches][0], true);
            if (e.key === "Escape") setFind("");
          }} />}
        {!bare && <button className="button narrow-only" aria-expanded={filtersOpen} onClick={() => setFiltersOpen(!filtersOpen)}><Funnel size={16} aria-hidden />Filters</button>}
        {!bare && <div className="graph-tools">
          {!asList && (
            <>
              <button className="button" onClick={() => fit()} title="Fit (0)"><ArrowsIn size={16} aria-hidden />Fit</button>
              <span className="zoom" role="group" aria-label="Zoom">
                <button className="icon-button" onClick={() => zoomBy(1 / 1.4)} aria-label="Zoom out" title="Zoom out (-)"><Minus size={16} /></button>
                <span className="num zoom-value">{Math.round(zoom * 100)}%</span>
                <button className="icon-button" onClick={() => zoomBy(1.4)} aria-label="Zoom in" title="Zoom in (+)"><Plus size={16} /></button>
              </span>
            </>
          )}
          <button className="button" aria-pressed={asList} onClick={() => setAsList(!asList)}>
            {asList ? <><GraphIcon size={16} aria-hidden />Show as graph</> : <><ListBullets size={16} aria-hidden />Show as list</>}
          </button>
        </div>}
      </header>
      {!bare && <div className={`graph-filters${filtersOpen ? " open" : ""}`} role="group" aria-label="Show">
        {(["client", "call", "transcript-chunk", "other", "missing", "unlinked"] as const).map((t) => (
          <label key={t} className="check">
            <input type="checkbox" checked={show[t]} onChange={(e) => setShow({ ...show, [t]: e.target.checked })} />
            {{ client: "Clients", call: "Meetings", "transcript-chunk": "Transcript parts", other: "Other notes",
              missing: "Missing notes", unlinked: "Unlinked notes" }[t]}
          </label>
        ))}
        <label className="check client-pick">
          <span>Client</span>
          <select className="select plain" value={client} onChange={(e) => { setClient(e.target.value); setSelected(null); }}>
            <option value="">All</option>
            {clients.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
      </div>}
      <div className="graph-main">
        <div className="graph-canvas" ref={box} tabIndex={0} onKeyDown={onKey}
          aria-label={`Graph of ${graphData.nodes.length} notes and ${pairs} links${sel ? `. Selected: ${sel.title}, ${sel.links} links` : ""}. Arrow keys move between linked notes, Enter opens. Show as list gives the same notes as a table.`}>
          {result === null || (result.key !== key && !data) ? <div className="graph-message"><div className="skeleton-row wide" /></div>
            : "error" in result ? <div className="graph-message"><p>The graph could not be loaded: {result.error}</p></div>
            : total === 0 && !filtered ? (
              <div className="graph-message">
                <GraphIcon size={24} aria-hidden className="empty-icon" />
                <p>The graph draws your notes and the links between them. It needs at least one meeting.</p>
                <button className="button" onClick={onImport}>Go to Import</button>
              </div>
            ) : graphData.nodes.length === 0 ? (
              <div className="graph-message">
                <p>Nothing to show with these filters.</p>
                <button className="button" onClick={resetFilters}>Reset filters</button>
              </div>
            ) : asList ? <GraphList nodes={graphData.nodes} onOpen={onOpen} />
            : size.width > 0 && (
              <ForceGraph2D<GraphNode, object> ref={fg} graphData={graphData} width={size.width} height={size.height}
                backgroundColor={C.ground} nodeId="id" nodeLabel={() => ""} nodeCanvasObject={paint} nodePointerAreaPaint={hitArea}
                linkColor={linkColour} linkWidth={1}
                linkLineDash={(l: Edge) => (selected && idOf(l.target) === selected && idOf(l.source) !== selected ? [4 / zoom, 3 / zoom] : null)}
                cooldownTicks={allPlaced ? 0 : reduced() ? 0 : 400} cooldownTime={1500}
                warmupTicks={allPlaced ? 0 : reduced() ? 120 : 40} d3AlphaDecay={0.04}
                onEngineStop={settled} onNodeHover={(n) => setHover(n ? (n as Node).id : null)} onNodeClick={(n) => clicked(n as Node)}
                onBackgroundClick={() => setSelected(null)} onZoomEnd={(t) => requestAnimationFrame(() => setZoom(t.k))}
                onNodeDragEnd={(n) => { n.fx = n.x; n.fy = n.y; }} minZoom={0.05} maxZoom={12} />
            )}
        </div>
        {sel && <NodePanel api={api} node={sel} vault={vault} near={near} nodes={graphData.nodes} focusOn={focusOn}
          onOpen={onOpen} onSelect={(id) => select(id, true)} onFocus={setFocusOn} onClose={() => setSelected(null)} />}
      </div>
      {!bare && <footer className="graph-foot">
        <span className="num">{graphData.nodes.length} notes · {pairs} links</span>
        {focusOn && <button className="link-button" onClick={() => setFocusOn(null)}><ArrowLeft size={14} aria-hidden />Back to full graph</button>}
      </footer>}
    </section>
  );
}

function excerpt(body: string, type: string): string {
  const text = (type === "call" ? section(body, "Summary") : null) ?? (type === "client" ? section(body, "Current Status") : null) ?? body;
  return text.split("\n").map((l) => l.replace(/^[#>*\-\s]+|\[\[[^\]|]*\|?|\]\]|\*\*/g, "").trim()).filter(Boolean).slice(0, 3).join(" ");
}

function NodePanel({ api, node, vault, near, nodes, focusOn, onOpen, onSelect, onFocus, onClose }: {
  api: Api | null; node: Node; vault: string; near: ReturnType<typeof neighbours>; nodes: Node[];
  focusOn: { id: string; depth: number } | null; onOpen: (p: string) => void; onSelect: (id: string) => void;
  onFocus: (f: { id: string; depth: number } | null) => void; onClose: () => void }) {
  const missing = node.type === "missing";
  const note = useNote(api, missing ? null : node.id);
  const ins = near.get(node.id)?.in ?? new Set<string>();
  const outs = near.get(node.id)?.out ?? new Set<string>();
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const linked = [...new Set([...outs, ...ins])].map((id) => byId.get(id)).filter((n): n is Node => !!n)
    .sort((a, b) => (b.date ?? "").localeCompare(a.date ?? "") || a.title.localeCompare(b.title));
  const local = focusOn?.id === node.id;
  return (
    <aside className="graph-panel" aria-label={`Selected: ${node.title}`}>
      <div className="panel-head">
        <h2 className="panel-title">{node.title}</h2>
        <button className="icon-button" onClick={onClose} aria-label="Clear the selection" title="Esc"><X size={16} aria-hidden /></button>
      </div>
      <p className="head-meta">{[TYPE_LABEL[node.type], node.client].filter(Boolean).join(" · ")}</p>
      <p className="head-meta num">{ins.size} links in · {outs.size} out</p>
      {missing ? <p className="side-note">No note has this name. A link points to it.</p>
        : note.state === "ok" ? <p className="panel-excerpt">{excerpt(note.value.body, node.type)}</p> : <div className="skeleton-row" />}
      <div className="panel-actions">
        {!missing && <button className="button primary" onClick={() => onOpen(node.id)}><FileText size={16} aria-hidden />Open</button>}
        {!missing && vault && <button className="button" onClick={() => openExternal(obsidianUrl(vault, node.id))}><ArrowSquareOut size={16} aria-hidden />Open in Obsidian</button>}
        {local ? (
          <span className="depth" role="group" aria-label="Link depth">
            <span>Depth</span>
            {[1, 2, 3].map((d) => <button key={d} className="chip" aria-pressed={focusOn.depth === d} onClick={() => onFocus({ id: node.id, depth: d })}>{d}</button>)}
          </span>
        ) : <button className="button" onClick={() => onFocus({ id: node.id, depth: 1 })}>Show only its links</button>}
      </div>
      <h3 className="side-title">Links<span className="num">{linked.length}</span></h3>
      <ul className="side-list panel-links">
        {linked.map((n) => (
          <li key={n.id}><button className="side-link" onClick={() => onSelect(n.id)}>
            <span className="side-link-title">{n.title}</span>
            <span className="side-date">{ins.has(n.id) && !outs.has(n.id) ? "links here" : TYPE_LABEL[n.type]}</span>
          </button></li>
        ))}
      </ul>
    </aside>
  );
}

type SortKey = "title" | "type" | "client" | "links";

function GraphList({ nodes, onOpen }: { nodes: GraphNode[]; onOpen: (p: string) => void }) {
  const [sort, setSort] = useState<{ key: SortKey; down: boolean }>({ key: "links", down: true });
  const rows = [...nodes].sort((a, b) => {
    const d = sort.key === "links" ? a.links - b.links : String(a[sort.key] ?? "").localeCompare(String(b[sort.key] ?? ""));
    return sort.down ? -d : d;
  });
  const head = (key: SortKey, label: string) => (
    <th aria-sort={sort.key === key ? (sort.down ? "descending" : "ascending") : "none"}>
      <button onClick={() => setSort({ key, down: sort.key === key ? !sort.down : key === "links" })}>{label}</button>
    </th>
  );
  return (
    <div className="graph-list">
      <table>
        <thead><tr>{head("title", "Note")}{head("type", "Type")}{head("client", "Client")}{head("links", "Links")}</tr></thead>
        <tbody>
          {rows.slice(0, 1000).map((n) => (
            <tr key={n.id}>
              <td>{n.type === "missing" ? <span className="muted">{n.title}</span> : <button className="link-button accent" onClick={() => onOpen(n.id)}>{n.title}</button>}</td>
              <td>{TYPE_LABEL[n.type]}</td><td>{n.client ?? ""}</td><td className="num">{n.links}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 1000 && <p className="side-note">The first 1,000 of {rows.length}, by the column you sorted. Filters narrow it.</p>}
    </div>
  );
}
