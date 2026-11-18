// The command palette's logic: what the typed text asks for, and the groups of rows it shows.

export type Piece = [string, boolean];
export type Hit = { path: string; title: string; type: string; client: string | null; date: string | null; snippet: Piece[] };
export type Command = { id: string; label: string; hint?: string };
export type Item =
  | { kind: "command"; id: string; label: string; hint?: string }
  | { kind: "client"; name: string; meta: string }
  | { kind: "note"; hit: Hit };
export type Group = { label: string; items: Item[] };

const PER_GROUP = 5;
const GROUPS: [string, (h: Hit) => boolean][] = [
  ["Meetings", (h) => h.type === "call"],
  ["Clients", (h) => h.type === "client"],
  ["Transcript", (h) => h.type === "transcript-chunk"],
  ["Notes", (h) => h.type !== "call" && h.type !== "client" && h.type !== "transcript-chunk"],
];

/** Lower case without diacritics, đ as d: the same folding the engine's index uses. */
export function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").replace(/[đĐ]/g, "d").toLowerCase();
}

/** `>` commands only, `@` clients only, else everything. */
export function parse(input: string): { mode: "all" | "commands" | "clients"; text: string } {
  const t = input.trimStart();
  if (t.startsWith(">")) return { mode: "commands", text: t.slice(1).trim() };
  if (t.startsWith("@")) return { mode: "clients", text: t.slice(1).trim() };
  return { mode: "all", text: t.trim() };
}

/** Every word of the query starts a word of the text (diacritics and dj/đ ignored). */
export function matches(text: string, query: string): boolean {
  const words = fold(text).split(/[^\p{L}\p{N}]+/u);
  return fold(query).split(/\s+/).filter(Boolean).every((q) =>
    words.some((w) => w.startsWith(q) || w.startsWith(q.replace(/dj/g, "d"))));
}

export type Client = { name: string; aliases: string[]; meetings: number; last_meeting: string | null };

export function groups(input: string, commands: Command[], clients: Client[], hits: Hit[] | null, recent: Hit[]): Group[] {
  const { mode, text } = parse(input);
  const cmds = commands.filter((c) => !text || matches(c.label, text)).map((c) => ({ kind: "command" as const, ...c }));
  if (mode === "commands") return cmds.length ? [{ label: "Commands", items: cmds }] : [];
  const people = clients.filter((c) => matches([c.name, ...c.aliases].join(" "), text))
    .map((c) => ({ kind: "client" as const, name: c.name, meta: c.meetings ? `${c.meetings} meetings` : "No meetings yet" }));
  if (mode === "clients") return people.length ? [{ label: "Clients", items: people.slice(0, 20) }] : [];
  if (!text) {
    return [...(recent.length ? [{ label: "Recent", items: recent.slice(0, PER_GROUP).map((hit) => ({ kind: "note" as const, hit })) }] : []),
      ...(cmds.length ? [{ label: "Commands", items: cmds }] : [])];
  }
  const out: Group[] = [];
  const listed = new Set(people.map((p) => p.name));
  for (const [label, test] of GROUPS) {
    // a client found by name is listed once, under Clients, as the client page
    const items: Item[] = (hits ?? []).filter(test).filter((h) => !(h.type === "client" && h.client && listed.has(h.client)))
      .slice(0, PER_GROUP).map((hit) => ({ kind: "note" as const, hit }));
    if (label === "Clients") items.unshift(...people.slice(0, PER_GROUP));
    if (items.length) out.push({ label, items: items.slice(0, PER_GROUP) });
  }
  if (cmds.length) out.push({ label: "Commands", items: cmds.slice(0, PER_GROUP) });
  return out;
}

/** Search by meaning can answer: the engine runs it and some notes have vectors. */
export function meaningAvailable(e: { state: string; done: number } | null | undefined): boolean {
  return !!e && ["ready", "indexing", "waiting"].includes(e.state) && e.done > 0;
}

export function flat(list: Group[]): Item[] {
  return list.flatMap((g) => g.items);
}
