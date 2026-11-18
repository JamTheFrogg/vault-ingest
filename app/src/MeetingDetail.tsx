import { CheckCircle, Circle, FlagPennant, Pause, Square, Waveform, WarningCircle } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import type { Api } from "./engine";
import { STEP_DETAIL, clock, hmsSeconds, length, type Row, type Session, type Status } from "./meetings";
import { Swatch } from "./MeetingsList";
import { bodyForDetail, partLinks, sections, type Note } from "./note";
import { NoteMarkdown, NoteReader, NoteState, ObsidianButton, useNote } from "./Reader";

/** `note`: a note opened from a link in this meeting, shown over it until Back. */
type Props = { row: Row | null; api: Api | null; status: Status | null; colours: Map<string, string>; now: Date;
  note: string | null; version: number; onOpen: (path: string) => void; onBack: () => void; onSearch: (text: string) => void;
  onStop: () => void; onMark: () => void };
export function MeetingDetail({ row, api, status, colours, now, note, version, onOpen: open, onBack, onSearch, onStop, onMark }: Props) {
  const vault = status?.vault ?? "";
  if (note) {
    return <NoteReader key={note} api={api} target={note} vault={vault} version={version} onOpen={open} onBack={onBack}
      backLabel={row ? `Back to ${row.title}` : "Back"} onSearch={onSearch} />;
  }
  if (!row) return <section className="detail detail-empty" aria-label="Meeting"><p>Choose a meeting on the left.</p></section>;
  return (
    <section className="detail" aria-label={`Meeting: ${row.title}`}>
      {row.stage === "ready" && row.call
        ? <CallDetail key={row.key} row={row} api={api} vault={vault} colours={colours} onOpen={open} />
        : row.stage === "recording" ? <RecordingDetail row={row} colours={colours} now={now} onStop={onStop} onMark={onMark} />
        : <PendingDetail key={row.key} row={row} api={api} status={status} colours={colours} now={now} onOpen={open} />}
    </section>
  );
}

function when(fm: Record<string, unknown>, session?: Session): string {
  const start = String(fm.meeting_start ?? session?.start ?? "");
  const end = String(fm.meeting_end ?? session?.end ?? "");
  const day = start ? new Date(start) : fm.date ? new Date(`${fm.date}T00:00`) : null;
  if (!day || Number.isNaN(day.getTime())) return String(fm.date ?? "");
  const date = day.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  if (!start) return date;
  const t = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${date}, ${t(start)}${end ? `–${t(end)}` : ""}`;
}

type Tick = { at: number; label: string; path?: string; kind: "part" | "shot" | "mark" };

function CallDetail({ row, api, vault, colours, onOpen }: { row: Row; api: Api | null; vault: string;
  colours: Map<string, string>; onOpen: (path: string) => void }) {
  const call = row.call!;
  const note = useNote(api, call.call_note, call.processed_at ?? "");
  const links = note.state === "ok" ? note.value.links : undefined;
  const [session, setSession] = useState<Session | undefined>(row.session);
  const [parts, setParts] = useState<Note[] | null>(null);
  const body = note.state === "ok" ? note.value.body : "";

  useEffect(() => {
    if (!api || !call.session_id) return;
    api.get<Session>(`/sessions/${encodeURIComponent(call.session_id)}`).then(setSession, () => {});
  }, [api, call.session_id]);
  useEffect(() => {
    if (!api || !body) return;
    let live = true;
    Promise.all(partLinks(body).map((p) => api.get<Note>("/notes", { path: p }).catch(() => null)))
      .then((list) => live && setParts(list.filter((x): x is Note => x !== null)));
    return () => { live = false; };
  }, [api, body]);

  if (note.state !== "ok") return <NoteState state={note} />;
  const fm = note.value.frontmatter;
  const people = Array.isArray(fm.participants) ? fm.participants.map(String) : [];
  const seconds = hmsSeconds(fm.duration) ?? session?.duration
    ?? hmsSeconds(parts?.at(-1)?.frontmatter.time_end) ?? null;
  const ticks: Tick[] = [
    ...(parts ?? []).map((p, i) => ({ at: hmsSeconds(p.frontmatter.time_start) ?? -1, label: `p${i + 1}`, path: p.path, kind: "part" as const })),
    ...(session?.screenshots ?? []).map((s) => ({ at: hmsSeconds(s.offset) ?? -1, label: `Screenshot at ${s.offset}`, kind: "shot" as const })),
    ...(session?.marks ?? []).map((m) => ({ at: hmsSeconds(m.offset) ?? -1, label: `Marked at ${m.offset}`, kind: "mark" as const })),
  ].filter((t) => t.at >= 0);
  const engines = [fm.generated_by && `notes: ${fm.generated_by}`, fm.transcribed_by && `transcript: ${fm.transcribed_by}`].filter(Boolean);
  const toc = sections(body);

  return (
    <>
      <header className="detail-head">
        <div className="head-row">
          <div className="head-text">
            <div className="head-client"><Swatch client={call.client} colours={colours} />{call.client}</div>
            <h1 className="head-title">{call.title}</h1>
          </div>
          <ObsidianButton vault={vault} path={call.call_note} />
        </div>
        <p className="head-meta">
          {[when(fm, session), seconds ? length(seconds) : "", session?.app ?? ""].filter(Boolean).join(" · ")}
        </p>
        {(people.length > 0 || engines.length > 0) && (
          <p className="head-meta">{[people.length ? `${people.join(", ")} (${people.length} ${people.length === 1 ? "person" : "people"})` : "", ...engines].filter(Boolean).join(" · ")}</p>
        )}
      </header>
      {seconds ? <TapeStrip seconds={seconds} ticks={ticks} onOpen={onOpen} /> : null}
      <nav className="toc-bar" aria-label="On this page">
        {toc.map((s) => <a key={s.id} href={`#${s.id}`}>{s.title}{s.count ? <span className="num">{s.count}</span> : null}</a>)}
      </nav>
      <div className="detail-body">
        <article className="reader"><NoteMarkdown body={bodyForDetail(body)} links={links} onOpen={onOpen} /></article>
        <aside className="side" aria-label="On this page">
          <h2 className="side-title">On this page</h2>
          <ul>
            {toc.map((s) => (
              <li key={s.id}><a href={`#${s.id}`}><span>{s.title}</span>{s.count ? <span className="num">{s.count}</span> : null}</a></li>
            ))}
          </ul>
          <h2 className="side-title">File</h2>
          <p className="path">{call.call_note}</p>
        </aside>
      </div>
    </>
  );
}

function TapeStrip({ seconds, ticks, onOpen }: { seconds: number; ticks: Tick[]; onOpen: (path: string) => void }) {
  const pct = (at: number) => `${Math.min(100, (at / seconds) * 100)}%`;
  return (
    <div className="tape" role="group" aria-label={`Meeting timeline, ${clock(seconds)} long`}>
      <span className="tape-time num">0:00</span>
      <div className="tape-track">
        <div className="tape-line" />
        {ticks.map((t) => t.kind === "part" ? (
          <button key={`p${t.at}${t.label}`} className="tick tick-part" style={{ left: pct(t.at) }}
            onClick={() => onOpen(t.path!)} aria-label={`Open part ${t.label.slice(1)}, starts at ${clock(t.at)}`}
            title={`Part ${t.label.slice(1)} · ${clock(t.at)}`}>
            <span className="tick-mark" /><span className="tick-label num">{t.label}</span>
          </button>
        ) : (
          <span key={`${t.kind}${t.at}`} className={`tick tick-${t.kind === "mark" ? "flag" : t.kind}`} style={{ left: pct(t.at) }} title={t.label} aria-label={t.label} role="img">
            <span className="tick-mark" />
          </span>
        ))}
      </div>
      <span className="tape-time num">{clock(seconds)}</span>
    </div>
  );
}

/** A meeting being recorded: the timer, its marks so far, Stop and Mark. */
function RecordingDetail({ row, colours, now, onStop, onMark }: { row: Row; colours: Map<string, string>; now: Date;
  onStop: () => void; onMark: () => void }) {
  const seconds = (now.getTime() - new Date(row.start!).getTime()) / 1000;
  const marks = row.session?.marks ?? [];
  return (
    <>
      <header className="detail-head">
        <div className="head-client"><Swatch client={row.client} colours={colours} />{row.client ?? "Client not chosen yet"}</div>
        <h1 className="head-title">{row.title}</h1>
        <p className="head-meta">{row.session?.app ?? "Recording"} · started {new Date(row.start!).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</p>
      </header>
      <div className="pending">
        <p className="pending-note tone-recording"><Circle size={16} weight="fill" className="rec-pulse" aria-hidden />
          <span>Recording <span className="num">{clock(seconds)}</span>{marks.length ? ` · ${marks.length} marked` : ""}</span></p>
        <div className="actions">
          <button className="button" onClick={onStop}><Square size={14} weight="fill" aria-hidden />Stop</button>
          <button className="button" onClick={onMark}><FlagPennant size={16} aria-hidden />Mark this moment</button>
        </div>
        <p className="muted">When you stop, the two tracks (you and the call) are transcribed and the note is written.
          The client can be chosen until then, in the call popup.</p>
      </div>
    </>
  );
}

const STEPS = ["Transcribing", "Writing notes", "Filing under a client"];
const STEP_INDEX: Record<string, number> = { transcribing: 0, summary: 1, evidence: 1, client: 2, profile: 2 };
const REVIEW_DIR = "00_Inbox/_needs-review";         // config.Settings.review_dir
const AUDIO = /\.(m4a|mp3|mp4|wav|ogg|opus|flac|aac|webm|mkv)$/i;

function PendingDetail({ row, api, status, colours, now, onOpen }: { row: Row; api: Api | null; status: Status | null;
  colours: Map<string, string>; now: Date; onOpen: (path: string) => void }) {
  const why = useNote(api, row.stage === "review" ? `${REVIEW_DIR}/${row.file}.why.md` : null);
  const job = status?.job;
  const gpu = status?.gpu;
  const current = row.stage === "working" && job ? STEP_INDEX[job.step] ?? -1 : -1;
  const steps = row.file && !AUDIO.test(row.file) ? STEPS.slice(1) : STEPS;
  const offset = steps.length < STEPS.length ? 1 : 0;

  return (
    <>
      <header className="detail-head">
        <div className="head-client"><Swatch client={row.client} colours={colours} />{row.client ?? "Unsorted"}</div>
        <h1 className="head-title">{row.title}</h1>
        <p className="head-meta">
          {row.stage === "live"
            ? `${row.line} · started ${new Date(row.start!).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })} · `
            : ""}
          {row.stage === "live" ? <span className="num">{clock((now.getTime() - new Date(row.start!).getTime()) / 1000)}</span>
            : <span className="path">{row.file}</span>}
        </p>
      </header>
      <div className="pending">
        {row.stage === "live" && (
          <p className="pending-note">
            <Circle size={16} aria-hidden /> A call is in progress and is not being recorded. Record it from the
            call popup or the Record button.
          </p>
        )}
        {row.stage === "review" && (
          <div className="review">
            <p className="review-flag tone-failed"><WarningCircle size={16} weight="fill" aria-hidden />Needs review</p>
            {why.state === "ok" ? <NoteMarkdown body={bodyForDetail(why.value.body)} onOpen={onOpen} />
              : why.state === "loading" ? null : <p className="muted">No reason was written for this file.</p>}
          </div>
        )}
        {(row.stage === "waiting" || row.stage === "working") && (
          <>
            {row.stage === "waiting" && (gpu?.paused
              ? <p className="pending-note tone-paused"><Pause size={16} weight="fill" aria-hidden />{row.line}. It starts when the pause ends.</p>
              : <p className="pending-note">Waiting in the inbox. It is processed in turn.</p>)}
            <ol className="steps">
              {steps.map((label, i) => {
                const n = i + offset;
                const state = current < 0 ? "todo" : n < current ? "done" : n === current ? "now" : "todo";
                return (
                  <li key={label} className={`step step-${state}`}>
                    {state === "done" ? <CheckCircle size={16} weight="fill" aria-hidden />
                      : state === "now" ? <Waveform size={16} weight="bold" aria-hidden /> : <Circle size={16} aria-hidden />}
                    <span>{label}{state === "now" && job ? <span className="step-detail">{STEP_DETAIL[job.step]}</span> : null}</span>
                    <span className="visually-hidden">{state === "done" ? "done" : state === "now" ? "in progress" : "not started"}</span>
                  </li>
                );
              })}
            </ol>
          </>
        )}
      </div>
    </>
  );
}
