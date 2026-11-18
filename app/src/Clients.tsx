// Clients: the list of client folders, and the client page (the profile, read for "where are we with them?").
import { ArrowRight, MagnifyingGlass, UsersThree } from "@phosphor-icons/react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { Api } from "./engine";
import type { GraphData, GraphNode } from "./graph";
import { Swatch } from "./MeetingsList";
import { section, withoutSections } from "./note";
import { fold } from "./palette";
import { NoteMarkdown, NoteState, ObsidianButton, useNote } from "./Reader";

export type ClientRow = { name: string; aliases: string[]; contacts: string[]; meetings: number; last_meeting: string | null;
  open_items: number; profile: string | null };
type Sort = "recent" | "name" | "open";

const APART = new Set(["personal", "unsorted"]);          // real client folders, listed at the end
const LEAD = ["Current Status", "Open Items"];
const PROFILE_TITLE = /^\s*# [^\n]*\n+/;

export function day(date: string | null): string {
  if (!date) return "";
  const d = new Date(`${date}T00:00`);
  if (Number.isNaN(d.getTime())) return date;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", ...(d.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}) });
}

export function sortClients(rows: ClientRow[], sort: Sort, filter: string): { main: ClientRow[]; apart: ClientRow[] } {
  const q = fold(filter.trim());
  const shown = rows.filter((r) => !q || [r.name, ...r.aliases, ...r.contacts].some((t) => fold(t).includes(q)));
  const by = {
    recent: (a: ClientRow, b: ClientRow) => (b.last_meeting ?? "").localeCompare(a.last_meeting ?? "") || a.name.localeCompare(b.name),
    name: (a: ClientRow, b: ClientRow) => a.name.localeCompare(b.name),
    open: (a: ClientRow, b: ClientRow) => b.open_items - a.open_items || a.name.localeCompare(b.name),
  }[sort];
  const sorted = [...shown].sort(by);
  return { main: sorted.filter((r) => !APART.has(r.name.toLowerCase())), apart: sorted.filter((r) => APART.has(r.name.toLowerCase())) };
}

function moveFocus(event: KeyboardEvent<HTMLElement>) {
  const down = event.key === "ArrowDown" || event.key === "j";
  const up = event.key === "ArrowUp" || event.key === "k";
  if (!down && !up) return;
  const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>(".row")];
  const next = buttons[buttons.indexOf(document.activeElement as HTMLButtonElement) + (down ? 1 : -1)];
  if (next) {
    event.preventDefault();
    next.focus();
  }
}

export function ClientsList({ rows, colours, selected, onSelect }: { rows: ClientRow[] | null; colours: Map<string, string>;
  selected: string | null; onSelect: (name: string) => void }) {
  const [sort, setSort] = useState<Sort>("recent");
  const [filter, setFilter] = useState("");
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      const typing = (e.target as HTMLElement).closest("input, textarea, select");
      if (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        field.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const { main, apart } = sortClients(rows ?? [], sort, filter);
  const chips: [Sort, string][] = [["recent", "Recent"], ["name", "Name"], ["open", "Open items"]];
  const row = (r: ClientRow) => (
    <li key={r.name}>
      <button className="row" aria-current={selected === r.name ? "true" : undefined} onClick={() => onSelect(r.name)}>
        <span className="row-top row-strong">
          <Swatch client={r.name} colours={colours} />
          <span className="row-client">{r.name}</span>
          {r.open_items > 0 && <span className="row-length num" aria-label={`${r.open_items} open items`}>{r.open_items} open</span>}
        </span>
        <span className="stage">{r.meetings
          ? `${day(r.last_meeting)} · ${r.meetings} ${r.meetings === 1 ? "meeting" : "meetings"}` : "No meetings yet"}</span>
      </button>
    </li>
  );
  return (
    <section className="list" aria-label="Clients">
      <header className="list-head">
        <h1 className="list-title">Clients</h1>
        <label className="filter">
          <MagnifyingGlass size={14} aria-hidden />
          <input ref={field} className="input filter-input" placeholder="Filter clients" value={filter} aria-label="Filter clients"
            onChange={(e) => setFilter(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setFilter(""); e.currentTarget.blur(); } }} />
          <kbd className="filter-key" aria-hidden>/</kbd>
        </label>
        <div className="chips" role="group" aria-label="Sort">
          {chips.map(([id, label]) => <button key={id} className="chip" aria-pressed={sort === id} onClick={() => setSort(id)}>{label}</button>)}
        </div>
      </header>
      <div className="list-body" onKeyDown={moveFocus}>
        {rows === null ? <div className="skeleton" aria-label="Loading clients">{[0, 1, 2, 3].map((i) => <div key={i} className="skeleton-row" />)}</div>
          : rows.length === 0 ? (
            <div className="list-empty first-use">
              <p>Clients are created the first time you file a meeting under a new name.</p>
              <p className="hint">The call popup asks for the client; each one gets a folder with its profile under <code>Clients</code>.</p>
            </div>
          ) : main.length + apart.length === 0 ? (
            <div className="list-empty"><p>No client matches “{filter}”.</p><button className="button" onClick={() => setFilter("")}>Clear the filter</button></div>
          ) : (
            <>
              <ul>{main.map(row)}</ul>
              {apart.length > 0 && <section aria-label="Other"><h2 className="group">Other</h2><ul>{apart.map(row)}</ul></section>}
            </>
          )}
      </div>
    </section>
  );
}

/** `client`: undefined while the list loads, null when no folder has that name. */
type PageProps = { api: Api | null; client: ClientRow | null | undefined; name: string | null; vault: string; colours: Map<string, string>;
  version: number; onOpen: (path: string) => void; onMeeting: (path: string) => void };

/** The client page: the profile's frontmatter as a header, where things stand, open items, the last meeting, the
 *  rest of the profile, and the client's meetings and other notes. */
export function ClientPage({ api, client, name, vault, colours, version, onOpen, onMeeting }: PageProps) {
  const profile = useNote(api, client?.profile ?? null, version);
  const [notes, setNotes] = useState<{ key: string; nodes: GraphNode[] } | null>(null);
  const key = `${name}|${version}|${client?.meetings}`;
  useEffect(() => {
    if (!api || !name) return;
    let live = true;
    api.get<GraphData>("/graph", { client: name, types: "call,other" }).then((g) => live && setNotes({ key, nodes: g.nodes }), () => {});
    return () => { live = false; };
  }, [api, name, key]);
  const nodes = notes?.key === key ? notes.nodes : null;
  const meetings = (nodes ?? []).filter((n) => n.type === "call").sort((a, b) => (b.date ?? "").localeCompare(a.date ?? "") || b.title.localeCompare(a.title));
  const others = (nodes ?? []).filter((n) => n.type !== "call" && n.id !== client?.profile).sort((a, b) => a.title.localeCompare(b.title));
  const last = useNote(api, meetings[0]?.id ?? null);

  if (!name) {
    return (
      <section className="detail detail-empty" aria-label="Client">
        <div className="empty-inline"><UsersThree size={24} aria-hidden className="empty-icon" /><p>Choose a client on the left.</p></div>
      </section>
    );
  }
  if (client === undefined) return <section className="detail" aria-label={name}><div className="pending"><NoteState state={{ state: "loading" }} /></div></section>;
  if (client === null) return <section className="detail" aria-label={name}><p className="detail-message">There is no client named {name}.</p></section>;

  const fm = profile.state === "ok" ? profile.value.frontmatter : {};
  const body = profile.state === "ok" ? profile.value.body.replace(PROFILE_TITLE, "") : "";
  const links = profile.state === "ok" ? profile.value.links : undefined;
  const status = section(body, "Current Status");
  const open = section(body, "Open Items");
  const rest = withoutSections(body, LEAD);
  const parts = rest.split(/^(?=## )/m).filter((p) => p.trim());
  const lastSummary = last.state === "ok" ? (section(last.value.body, "Summary") ?? "").split("\n").filter(Boolean).slice(0, 3).join("\n") : "";
  const facts = [fm.status ? String(fm.status) : "", fm.last_contact ? `last contact ${day(String(fm.last_contact))}` : "",
    client.contacts.length ? `${client.contacts.join(", ")} (contact)` : ""].filter(Boolean);

  return (
    <section className="detail client-page" aria-label={name}>
      <header className="detail-head">
        <div className="head-row">
          <div className="head-text">
            <h1 className="head-title with-swatch"><Swatch client={name} colours={colours} />{name}</h1>
            {facts.length > 0 && <p className="head-meta">{facts.join(" · ")}</p>}
            {client.aliases.length > 0 && <p className="head-meta">Also known as {client.aliases.join(", ")}</p>}
          </div>
          {client.profile && <ObsidianButton vault={vault} path={client.profile} />}
        </div>
      </header>
      <div className="detail-body reader-body">
        <article className="reader client-main">
          {!client.profile ? <p className="detail-message flush">This client has no profile note yet. It is written with the first meeting.</p>
            : profile.state !== "ok" ? <NoteState state={profile} /> : (
              <>
                {(status !== null || parts.length === 0) && (
                  <section aria-labelledby="where">
                    <h2 id="where">Where things stand</h2>
                    {status ? <NoteMarkdown body={status} links={links} onOpen={onOpen} /> : <p className="muted">Nothing written yet.</p>}
                  </section>
                )}
                {open !== null && (
                  <section aria-labelledby="open">
                    <h2 id="open">Open items{client.open_items ? <span className="h-count num">{client.open_items}</span> : null}</h2>
                    {open && !/^_None\._$/.test(open) ? <NoteMarkdown body={open} links={links} onOpen={onOpen} /> : <p className="muted">None open.</p>}
                  </section>
                )}
                {meetings[0] && (
                  <section aria-labelledby="last">
                    <h2 id="last">Last meeting<span className="h-meta"> · {day(meetings[0].date)} · {meetings[0].title.replace(/^\d{4}-\d{2}-\d{2} /, "")}</span></h2>
                    {lastSummary ? <p>{lastSummary}</p> : null}
                    <button className="link-button accent" onClick={() => onMeeting(meetings[0].id)}>Open the meeting<ArrowRight size={14} aria-hidden /></button>
                  </section>
                )}
                {parts.map((p) => {
                  const title = /^## (.+)$/m.exec(p)?.[1].trim();
                  const inner = title ? p.replace(/^## .+\n?/, "") : p;
                  if (!title) return <NoteMarkdown key="intro" body={inner} links={links} onOpen={onOpen} />;
                  return (
                    <details key={title} className="profile-section">
                      <summary><h2>{title}</h2></summary>
                      {inner.trim() ? <NoteMarkdown body={inner} links={links} onOpen={onOpen} /> : <p className="muted">Empty.</p>}
                    </details>
                  );
                })}
              </>
            )}
        </article>
        <aside className="note-side" aria-label={`${name}: meetings and notes`}>
          <h2 className="side-title">Meetings{meetings.length ? <span className="num">{meetings.length}</span> : null}</h2>
          {nodes === null ? <p className="side-note">Loading…</p> : meetings.length === 0
            ? <p className="side-note">No meetings with {name} yet. The next one you file here will fill this page.</p> : (
              <ul className="side-list timeline">
                {meetings.map((m) => (
                  <li key={m.id}>
                    <button className="side-link" onClick={() => onMeeting(m.id)}>
                      <span className="side-date num">{day(m.date)}</span>
                      <span className="side-link-title">{m.title.replace(/^\d{4}-\d{2}-\d{2} /, "")}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          {others.length > 0 && (
            <>
              <h2 className="side-title">Notes<span className="num">{others.length}</span></h2>
              <ul className="side-list">
                {others.map((n) => <li key={n.id}><button className="side-link" onClick={() => onOpen(n.id)}><span className="side-link-title">{n.title}</span></button></li>)}
              </ul>
            </>
          )}
        </aside>
      </div>
    </section>
  );
}
