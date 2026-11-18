import { Circle, Pause, Square, Waveform, WarningCircle } from "@phosphor-icons/react";
import { useState, type KeyboardEvent } from "react";
import type { Conn } from "./App";
import { clock, groupRows, length, matches, type Filter, type Row } from "./meetings";

type Props = {
  rows: Row[] | null; conn: Conn; now: Date; colours: Map<string, string>;
  selected: string | null; onSelect: (key: string) => void; onStop: () => void;
};

export function Swatch({ client, colours }: { client: string | null; colours: Map<string, string> }) {
  return <span className="swatch" style={{ background: client ? colours.get(client) : undefined }} aria-hidden />;
}

export function StageLine({ row, now }: { row: Row; now: Date }) {
  switch (row.stage) {
    case "recording":
      return <span className="stage tone-recording"><Circle size={10} weight="fill" className="rec-pulse" aria-hidden />Recording · <span className="num">
        {clock((now.getTime() - new Date(row.start!).getTime()) / 1000)}</span></span>;
    case "live":
      return <span className="stage tone-muted"><Circle size={12} aria-hidden />Call detected · <span className="num">
        {clock((now.getTime() - new Date(row.start!).getTime()) / 1000)}</span></span>;
    case "working":
      return <span className="stage tone-working"><Waveform size={12} weight="bold" aria-hidden />{row.line}</span>;
    case "review":
      return <span className="stage tone-failed"><WarningCircle size={12} weight="fill" aria-hidden />{row.line}</span>;
    case "waiting":
      return row.line.startsWith("Paused")
        ? <span className="stage tone-paused"><Pause size={12} weight="fill" aria-hidden />{row.line}</span>
        : <span className="stage tone-muted">{row.line}</span>;
    default:
      return <span className="stage">{row.line}</span>;
  }
}

function moveFocus(event: KeyboardEvent<HTMLElement>) {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>(".row")];
  const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const next = buttons[i + (event.key === "ArrowDown" ? 1 : -1)];
  if (next) {
    event.preventDefault();
    next.focus();
  }
}

export function MeetingsList({ rows, conn, now, colours, selected, onSelect, onStop }: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const all = rows ?? [];
  const counts = { working: all.filter((r) => matches(r, "working")).length, review: all.filter((r) => matches(r, "review")).length };
  const shown = all.filter((r) => matches(r, filter));
  const chips: [Filter, string, number | null][] = [["all", "All", null], ["working", "Working", counts.working], ["review", "Review", counts.review]];

  return (
    <section className="list" aria-label="Meetings">
      <header className="list-head">
        <h1 className="list-title">Meetings</h1>
        <div className="chips" role="group" aria-label="Show">
          {chips.map(([id, label, n]) => (
            <button key={id} className="chip" aria-pressed={filter === id} onClick={() => setFilter(id)}>
              {label}{n ? <span className="num chip-count">{n}</span> : null}
            </button>
          ))}
        </div>
      </header>
      <div className="list-body" onKeyDown={moveFocus}>
        {rows === null ? <ListSkeleton conn={conn} />
          : all.length === 0 ? <FirstUse />
          : shown.length === 0 ? (
            <div className="list-empty">
              <p>No meetings match.</p>
              <button className="button" onClick={() => setFilter("all")}>Show all</button>
            </div>
          ) : groupRows(shown, now).map((group) => (
            <section key={group.label} aria-label={group.label}>
              <h2 className="group">{group.label}</h2>
              <ul>
                {group.rows.map((row) => (
                  <li key={row.key} className="row-item">
                    <button className="row" aria-current={selected === row.key ? "true" : undefined} onClick={() => onSelect(row.key)}>
                      <span className="row-top">
                        <Swatch client={row.client} colours={colours} />
                        <span className="row-client">{row.client ?? "Unsorted"}</span>
                        {row.seconds ? <span className="row-length num">{length(row.seconds)}</span> : null}
                      </span>
                      <span className="row-title">{row.title}</span>
                      <StageLine row={row} now={now} />
                    </button>
                    {row.stage === "recording" && (
                      <button className="row-action" onClick={onStop} aria-label={`Stop recording ${row.title}`}>
                        <Square size={12} weight="fill" aria-hidden />Stop</button>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          ))}
      </div>
    </section>
  );
}

function ListSkeleton({ conn }: { conn: Conn }) {
  if (conn.state === "stopped") return <div className="list-empty"><p>The list appears when the engine runs.</p></div>;
  return <div className="skeleton" aria-label="Loading meetings">{[0, 1, 2, 3].map((i) => <div key={i} className="skeleton-row" />)}</div>;
}

function FirstUse() {
  return (
    <div className="list-empty first-use">
      <p>Your meetings will appear here, each with its note.</p>
      <p className="hint">To make the first one, put a transcript named <code>Client - YYYY-MM-DD - Title.txt</code> into
        your vault's <code>00_Inbox</code> folder.</p>
    </div>
  );
}
