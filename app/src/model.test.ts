import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { buildRows, groupRows, hmsSeconds, matches, parseInboxName, type MeetingsData, type Status } from "./meetings";
import { statusLine } from "./Rail";
import type { Api } from "./engine";
import { bodyForDetail, partLinks, remarkCallouts, sections, wikiToMarkdown } from "./note";

const NOW = new Date(2026, 10, 15, 16, 0);
const status = (over: Partial<Status> = {}): Status => ({
  vault: "/v", mode: "local", model: "m", inbox: [], review: [], watcher: null,
  job: { file: "", step: "", last_error: "" },
  gpu: { paused: false, reason: "", manual: false, until: null, phase: "idle", inbox: 0, recordings: 0 }, ...over,
});
const data: MeetingsData = {
  calls: [{ client: "Acme", date: "2026-11-14", title: "Kickoff", call_note: "Clients/Acme/Calls/k.md", chunks: 6,
    processed_at: "2026-11-14T15:00:00", generated_by: "cloud", screenshots: 1, session_id: "m1" }],
  sessions: [
    { id: "m1", app: "Google Meet", window_title: "", start: "2026-11-14T14:00:00", end: "2026-11-14T14:42:00",
      duration: 2520, screenshots: [], client: "Acme", call_title: "Kickoff", skip: false },
    { id: "m2", app: "Zoom", window_title: "", start: "2026-11-15T15:50:00", end: null, duration: 600,
      screenshots: [], client: null, call_title: null, skip: false },
  ],
  waiting: ["Acme - 2026-11-15 - Pricing.txt", "Voice 002.m4a"],
};

describe("meetings list", () => {
  it("shows every stage, grouped by day, unfinished work first", () => {
    const rows = buildRows(data, status({ review: ["Bolt - 2026-11-10 - Site.txt", "Bolt - 2026-11-10 - Site.txt.why.md"],
      job: { file: "Acme - 2026-11-15 - Pricing.txt", step: "evidence", last_error: "" } }), NOW);
    const groups = groupRows(rows, NOW);
    expect(groups.map((g) => g.label)).toEqual(["Today", "Yesterday", "This week"]);
    expect(groups[0].rows.map((r) => [r.stage, r.title, r.line])).toEqual([
      ["live", "Zoom", "Zoom"],
      ["working", "Pricing", "Writing notes, step 2 of 3"],
      ["waiting", "Voice 002", "Waiting"],
    ]);
    expect(groups[1].rows[0]).toMatchObject({ stage: "ready", client: "Acme", line: "6 parts · 1 screenshot", seconds: 2520 });
    expect(groups[2].rows.map((r) => [r.stage, r.line])).toEqual([["review", "Needs review"]]);   // the .why.md is not a row
  });

  it("says why waiting files wait and filters by stage", () => {
    const rows = buildRows(data, status({ gpu: { ...status().gpu, paused: true, reason: "a game is running" } }), NOW);
    expect(rows.filter((r) => r.stage === "waiting").map((r) => r.line)).toEqual(
      ["Paused: a game is running", "Paused: a game is running"]);
    expect(rows.filter((r) => matches(r, "working")).length).toBe(3);
    const byHand = buildRows(data, status({ gpu: { ...status().gpu, paused: true, manual: true, reason: "paused by hand" } }), NOW);
    expect(byHand.find((r) => r.stage === "waiting")?.line).toBe("Paused by you");
    expect(rows.filter((r) => matches(r, "review")).length).toBe(0);
  });

  it("reads inbox names and times", () => {
    expect(parseInboxName("Acme d.o.o. - 2026-09-30 - Kick - off.txt")).toEqual(
      { client: "Acme d.o.o.", date: "2026-09-30", title: "Kick - off" });
    expect(parseInboxName("memo.m4a")).toEqual({ client: null, date: null, title: "memo" });
    expect(hmsSeconds("00:42:10")).toBe(2530);
    expect(hmsSeconds("14:02")).toBe(842);
    expect(hmsSeconds("soon")).toBeNull();
  });
});

const BODY = `
# 2026-11-14 Kickoff

**Client:** [[Clients/Acme/Acme|Acme]] · **Date:** 2026-11-14

## Decisions
- Launch moves to 15 Oct

> [!quote]- Evidence
> - Launch: "let's say the fifteenth"

## Open Questions
_None._

## Transcript
- [[Clients/Acme/Transcripts/K/K - Part 01|Part 1]]
- [[Clients/Acme/Transcripts/K/K - Part 02|Part 2]]
`;

describe("note", () => {
  it("turns wikilinks into app links, but not inside code", () => {
    expect(wikiToMarkdown("See [[Clients/Acme/Acme|Acme]] and ![[a/shot 1.png]] `[[raw]]`")).toBe(
      "See [Acme](vault:Clients/Acme/Acme.md) and *Image: shot 1.png* `[[raw]]`");
    expect(wikiToMarkdown("[[Note#Heading]]")).toBe("[Note](vault:Note.md#s-heading)");
  });

  it("lists sections with counts, parts in order, and drops the repeated title", () => {
    expect(sections(BODY)).toEqual([
      { id: "s-decisions", title: "Decisions", count: 1 },
      { id: "s-open-questions", title: "Open Questions", count: null },
      { id: "s-transcript", title: "Transcript", count: 2 },
    ]);
    expect(partLinks(BODY)).toEqual(["Clients/Acme/Transcripts/K/K - Part 01.md", "Clients/Acme/Transcripts/K/K - Part 02.md"]);
    expect(bodyForDetail(BODY).startsWith("## Decisions")).toBe(true);
  });

  it("renders a collapsed callout as details", () => {
    const html = renderToStaticMarkup(createElement(Markdown, { remarkPlugins: [remarkCallouts] },
      "> [!note]- Text in image\n> PRICING 49 EUR"));
    expect(html.replace(/\n/g, "")).toBe('<details class="callout callout-note"><summary>Text in image</summary><p>PRICING 49 EUR</p></details>');
  });
});

describe("status area", () => {
  const ready = { state: "ready" as const, api: {} as Api };
  const line = (s: Status, rows = buildRows(data, s, NOW)) => {
    const { state, detail, tone } = statusLine(ready, s, rows);
    return [tone, state, detail];
  };
  it("says the most important state and one detail", () => {
    expect(statusLine({ state: "stopped", error: "x" }, null, []).state).toBe("Engine stopped");
    expect(line(status({ gpu: { ...status().gpu, paused: true, reason: "a game is running" } })))
      .toEqual(["paused", "Paused: a game is running", "2 waiting"]);
    expect(line(status({ job: { file: "Acme - 2026-11-15 - Pricing.txt", step: "summary", last_error: "" } })))
      .toEqual(["working", "Writing notes, step 1 of 3", "Pricing · 1 waiting"]);
    expect(line(status())).toEqual(["muted", "Call detected · Zoom", "2 waiting"]);
    const quiet = { ...data, sessions: [], waiting: [] };
    expect(line(status({ review: ["a.txt"] }), buildRows(quiet, status({ review: ["a.txt"] }), NOW)))
      .toEqual(["failed", "1 meeting to review", ""]);
    expect(line(status(), buildRows(quiet, status(), NOW))).toEqual(["muted", "Nothing running", "Every meeting has a note"]);
  });
});
