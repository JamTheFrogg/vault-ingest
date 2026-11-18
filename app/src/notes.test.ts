import { describe, expect, it } from "vitest";
import { sortClients, type ClientRow } from "./Clients";
import { DEFAULT_SHOW, graphQuery, local, radius, step, visible, type GraphData } from "./graph";
import { section, splitTarget, wikiToMarkdown, withoutSections, type Link } from "./note";
import { fold, groups, matches, parse, type Hit } from "./palette";
import { plainLinks, uniqueLinks } from "./Reader";

const link = (target: string, path: string | null, over: Partial<Link> = {}): Link =>
  ({ target, heading: null, alias: null, embed: false, attachment: false, path, ...over });

describe("wikilinks with the engine's resolution", () => {
  it("uses the resolved path for short names and aliases, marks missing notes, keeps headings", () => {
    const links = [link("Kickoff", "Clients/Acme/Calls/2026-09-30 Kickoff.md"), link("Gone", null), link("Forest", "Clients/Šumar/Šumar.md")];
    expect(wikiToMarkdown("[[Kickoff#Next Steps|the call]] · [[Gone]] · ![[Forest]] · ![[shot.png]] · [[spec.pdf]]", links)).toBe(
      "[the call](vault:Clients/Acme/Calls/2026-09-30%20Kickoff.md#s-next-steps) · [Gone](vault-missing:Gone) · "
      + "[Forest](vault:Clients/%C5%A0umar/%C5%A0umar.md) · *Image: shot.png* · spec.pdf");
    expect(wikiToMarkdown("[[Note (2)]]", [link("Note (2)", "Note (2).md")])).toBe("[Note (2)](vault:Note%20%282%29.md)");
    expect(wikiToMarkdown("[[Acme d.o.o]]")).toBe("[Acme d.o.o](vault:Acme%20d.o.o.md)");      // a dot in a name is not a file type
    expect(splitTarget("a/b.md#s-x")).toEqual({ path: "a/b.md", anchor: "s-x" });
    expect(splitTarget("a/b.md")).toEqual({ path: "a/b.md", anchor: null });
  });

  it("lists each linked note once and quotes backlinks without link syntax", () => {
    const list = uniqueLinks([link("A", "A.md"), link("a", "A.md"), link("x.png", null, { attachment: true }), link("Gone", null), link("gone", null)]);
    expect(list.map((l) => l.path ?? l.target)).toEqual(["A.md", "Gone"]);
    expect(plainLinks("- **Client:** [[Clients/Acme/Acme|Acme]] and [[Clients/Acme/Calls/K#Sum]]")).toBe("- Client: Acme and K");
  });
});

describe("profile sections", () => {
  const body = "## Snapshot\nWho.\n\n## Current Status\nWaiting.\n\n## Open Items\n- [ ] One\n\n## Work Log\n- x\n";
  it("finds a section and drops some", () => {
    expect(section(body, "current status")).toBe("Waiting.");
    expect(section(body, "Nope")).toBeNull();
    expect(withoutSections(body, ["Current Status", "Open Items"])).toBe("## Snapshot\nWho.\n\n## Work Log\n- x\n");
  });
});

describe("graph", () => {
  const data: GraphData = {
    nodes: [
      { id: "c", title: "Acme", type: "client", client: "Acme", date: null, links: 2 },
      { id: "m", title: "Kickoff", type: "call", client: "Acme", date: "2026-09-30", links: 2 },
      { id: "p", title: "Part 1", type: "transcript-chunk", client: "Acme", date: null, links: 1 },
      { id: "o", title: "Ideas", type: "other", client: null, date: null, links: 1 },
      { id: "lone", title: "Lone", type: "other", client: null, date: null, links: 0 },
    ],
    edges: [{ source: "m", target: "c" }, { source: "o", target: "c" }, { source: "p", target: "m" }],
  };
  it("asks the engine only for what is shown and hides unlinked notes by default", () => {
    expect(graphQuery(DEFAULT_SHOW, "")).toEqual({ types: "client,call,other" });
    expect(graphQuery({ ...DEFAULT_SHOW, "transcript-chunk": true, missing: true }, "Acme")).toEqual(
      { types: "client,call,transcript-chunk,other", client: "Acme", missing: "1" });
    expect(visible(data, DEFAULT_SHOW).nodes.map((n) => n.id)).toEqual(["c", "m", "o"]);
    expect(visible(data, { ...DEFAULT_SHOW, unlinked: true, "transcript-chunk": true }).edges).toHaveLength(3);
  });
  it("walks the local graph to a depth, sizes nodes by type, moves by direction", () => {
    expect(local(data, "p", 1).nodes.map((n) => n.id)).toEqual(["m", "p"]);
    expect(local(data, "p", 2).nodes.map((n) => n.id)).toEqual(["c", "m", "p"]);
    expect(radius(data.nodes[0])).toBeGreaterThan(radius(data.nodes[1]));
    expect(radius(data.nodes[1])).toBeGreaterThan(radius(data.nodes[2]));
    const near = [{ id: "r", x: 10, y: 1 }, { id: "l", x: -10, y: 0 }, { id: "u", x: 0, y: -10 }];
    expect(step({ x: 0, y: 0 }, near, "right")).toBe("r");
    expect(step({ x: 0, y: 0 }, near, "up")).toBe("u");
    expect(step({ x: 0, y: 0 }, near, "down")).toBeNull();
  });
});

describe("palette", () => {
  const hit = (path: string, type: string, client: string | null = null): Hit =>
    ({ path, title: path.split("/").at(-1)!.replace(".md", ""), type, client, date: null, snippet: [] });
  const clients = [{ name: "Šumar", aliases: ["Forest"], meetings: 3, last_meeting: null }, { name: "Acme", aliases: [], meetings: 0, last_meeting: null }];
  const commands = [{ id: "go-graph", label: "Go to Graph" }, { id: "record", label: "Start recording" }];

  it("folds like the engine and matches word prefixes", () => {
    expect(fold("Čćđšž ĐORĐE")).toBe("ccdsz dorde");
    expect(matches("Šumar d.o.o.", "sum")).toBe(true);
    expect(matches("Đorđe Petrović", "djordje pet")).toBe(true);
    expect(matches("Acme", "cme")).toBe(false);
    expect(parse(" >go")).toEqual({ mode: "commands", text: "go" });
    expect(parse("@su")).toEqual({ mode: "clients", text: "su" });
  });

  it("groups results, lists a client once, and narrows with prefixes", () => {
    const hits = [hit("Clients/Šumar/Šumar.md", "client", "Šumar"), hit("Clients/Šumar/Calls/K.md", "call", "Šumar"),
      hit("Clients/Šumar/Transcripts/K/K - Part 01.md", "transcript-chunk", "Šumar"), hit("Work/Ideas.md", "other")];
    const all = groups("sumar", commands, clients, hits, []);
    expect(all.map((g) => [g.label, g.items.length])).toEqual([["Meetings", 1], ["Clients", 1], ["Transcript", 1], ["Notes", 1]]);
    expect(all[1].items[0]).toEqual({ kind: "client", name: "Šumar", meta: "3 meetings" });
    expect(groups("forest", commands, clients, [], [])[0].items[0]).toMatchObject({ kind: "client", name: "Šumar" });
    expect(groups(">graph", commands, clients, hits, []).map((g) => g.label)).toEqual(["Commands"]);
    expect(groups("@", commands, clients, hits, [])[0].items).toHaveLength(2);
    expect(groups("", commands, clients, null, [hit("Work/Ideas.md", "other")]).map((g) => g.label)).toEqual(["Recent", "Commands"]);
    expect(groups("zzz", commands, clients, [], [])).toEqual([]);
  });
});

describe("clients list", () => {
  const row = (name: string, last: string | null, open = 0): ClientRow =>
    ({ name, aliases: [], contacts: [], meetings: last ? 1 : 0, last_meeting: last, open_items: open, profile: null });
  it("sorts by last meeting, name or open items and keeps Personal and Unsorted apart", () => {
    const rows = [row("Bolt", "2026-11-12", 1), row("Acme", "2026-11-14"), row("Unsorted", "2026-11-15"), row("Čelik", null, 4)];
    expect(sortClients(rows, "recent", "").main.map((r) => r.name)).toEqual(["Acme", "Bolt", "Čelik"]);
    expect(sortClients(rows, "open", "").main.map((r) => r.name)).toEqual(["Čelik", "Bolt", "Acme"]);
    expect(sortClients(rows, "name", "").apart.map((r) => r.name)).toEqual(["Unsorted"]);
    expect(sortClients(rows, "name", "cel").main.map((r) => r.name)).toEqual(["Čelik"]);
  });
});
