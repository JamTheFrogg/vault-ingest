// The graph's data: what /graph returns, the filters the screen applies, and the walks the panel and keys need.

export type NodeType = "client" | "call" | "transcript-chunk" | "other" | "missing";
export type GraphNode = { id: string; title: string; type: NodeType; client: string | null; date: string | null; links: number };
export type GraphEdge = { source: string; target: string };
export type GraphData = { nodes: GraphNode[]; edges: GraphEdge[] };
export type Show = { client: boolean; call: boolean; "transcript-chunk": boolean; other: boolean; missing: boolean; unlinked: boolean };

export const DEFAULT_SHOW: Show = { client: true, call: true, "transcript-chunk": false, other: true, missing: false, unlinked: false };
export const TYPE_LABEL: Record<NodeType, string> = {
  client: "Client profile", call: "Meeting note", "transcript-chunk": "Transcript part", other: "Note", missing: "Missing note",
};

/** The /graph query for what is shown: the engine leaves out types (and clients) the screen does not ask for. */
export function graphQuery(show: Show, client: string): Record<string, string> {
  const types = (["client", "call", "transcript-chunk", "other"] as const).filter((t) => show[t]);
  return { types: types.join(",") || "client", ...(client ? { client } : {}), ...(show.missing ? { missing: "1" } : {}) };
}

/** Applies the type and "unlinked" filters to what the engine returned (it may hold more than is shown). */
export function visible(data: GraphData, show: Show): GraphData {
  const nodes = data.nodes.filter((n) => show[n.type] && (show.unlinked || n.links > 0 || n.type === "client"));
  const ids = new Set(nodes.map((n) => n.id));
  return { nodes, edges: data.edges.filter((e) => ids.has(e.source) && ids.has(e.target)) };
}

export function neighbours(edges: GraphEdge[]): Map<string, { out: Set<string>; in: Set<string> }> {
  const map = new Map<string, { out: Set<string>; in: Set<string> }>();
  const get = (id: string) => map.get(id) ?? map.set(id, { out: new Set(), in: new Set() }).get(id)!;
  for (const e of edges) {
    get(e.source).out.add(e.target);
    get(e.target).in.add(e.source);
  }
  return map;
}

/** "Show only its links": the node and everything within `depth` links of it, either direction. */
export function local(data: GraphData, id: string, depth: number): GraphData {
  const near = neighbours(data.edges);
  const keep = new Set([id]);
  let ring = [id];
  for (let d = 0; d < depth; d++) {
    const next: string[] = [];
    for (const n of ring) {
      for (const m of [...(near.get(n)?.out ?? []), ...(near.get(n)?.in ?? [])]) {
        if (!keep.has(m)) {
          keep.add(m);
          next.push(m);
        }
      }
    }
    ring = next;
  }
  return { nodes: data.nodes.filter((n) => keep.has(n.id)), edges: data.edges.filter((e) => keep.has(e.source) && keep.has(e.target)) };
}

/** Radius in graph units: type sets the size, links add a little (DESIGN-direction.md 3, "The graph"). */
export function radius(node: GraphNode): number {
  const base = node.type === "client" ? 10 : node.type === "call" ? 4.5 : node.type === "transcript-chunk" ? 2 : 3.5;
  return base + Math.min(4, Math.sqrt(node.links) * (node.type === "client" ? 0.6 : 0.4));
}

/** Arrow keys on a selected node: the linked node that lies most in that direction. */
export function step(from: { x: number; y: number }, candidates: { id: string; x: number; y: number }[],
  dir: "left" | "right" | "up" | "down"): string | null {
  const [ux, uy] = { left: [-1, 0], right: [1, 0], up: [0, -1], down: [0, 1] }[dir];
  let best: string | null = null;
  let score = -Infinity;
  for (const c of candidates) {
    const dx = c.x - from.x;
    const dy = c.y - from.y;
    const dist = Math.hypot(dx, dy) || 1;
    const along = (dx * ux + dy * uy) / dist;
    if (along <= 0.3) continue;                        // roughly in that direction only
    const s = along * 2 - dist / 1000;
    if (s > score) {
      score = s;
      best = c.id;
    }
  }
  return best;
}
