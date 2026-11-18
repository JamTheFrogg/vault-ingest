import {
  Circle, DownloadSimple, Gear, Graph, HourglassMedium, ListBullets, MagnifyingGlass, Pause, Record, Square, UsersThree, Waveform,
  WarningCircle, type Icon,
} from "@phosphor-icons/react";
import type { Conn } from "./App";
import { STEPS, clock, parseInboxName, type Row, type Status } from "./meetings";
import { recordingNow, type Recording } from "./recording";

export type View = "meetings" | "clients" | "graph" | "import" | "settings";
const DESTINATIONS: { id: View; label: string; icon: Icon }[] = [
  { id: "meetings", label: "Meetings", icon: ListBullets },
  { id: "clients", label: "Clients", icon: UsersThree },
  { id: "graph", label: "Graph", icon: Graph },
  { id: "import", label: "Import", icon: DownloadSimple },
];

type Props = { view: View; onView: (v: View) => void; conn: Conn; status: Status | null; rows: Row[];
  recording: Recording | null; now: Date; onRecord: () => void; onStop: () => void; onSearch: () => void; download?: string | null };

const MOD = /Mac/.test(navigator.platform) ? "⌘" : "Ctrl";

export function Rail({ view, onView, conn, status, rows, recording, now, onRecord, onStop, onSearch, download = null }: Props) {
  const live = recordingNow(recording);
  const seconds = live ? (now.getTime() - new Date(recording.started_at).getTime()) / 1000 : 0;
  return (
    <div className="rail">
      <div className="wordmark"><Mark /><span className="wordmark-text">vault-ingest</span></div>
      <div className="rail-record">
        {live ? (
          <button className="record-control recording" onClick={onStop} title="Stop recording"
            aria-label={`Stop recording, ${clock(seconds)}`}>
            <Square size={16} weight="fill" aria-hidden />
            <span className="nav-label">Stop</span>
            <span className="nav-label record-time num">{clock(seconds)}</span>
          </button>
        ) : (
          <button className="record-control" onClick={onRecord} disabled={conn.state !== "ready"} title="Record"
            aria-label="Record">
            <Record size={16} weight="fill" aria-hidden /><span className="nav-label">Record</span>
          </button>
        )}
      </div>
      <div className="rail-search">
        <button className="nav-item search-item" onClick={onSearch} title={`Search (${MOD}+K)`} aria-label="Search" aria-keyshortcuts="Control+K Meta+K">
          <MagnifyingGlass size={20} aria-hidden />
          <span className="nav-label">Search</span>
          <kbd className="nav-label search-key">{MOD} K</kbd>
        </button>
      </div>
      <nav aria-label="Destinations">
        <ul className="nav">
          {DESTINATIONS.map(({ id, label, icon: NavIcon }) => (
            <li key={id}>
              <button className="nav-item" aria-current={view === id ? "page" : undefined} title={label}
                aria-label={label} onClick={() => onView(id)}>
                <NavIcon size={20} weight={view === id ? "fill" : "regular"} aria-hidden />
                <span className="nav-label">{label}</span>
              </button>
            </li>
          ))}
        </ul>
      </nav>
      <StatusArea conn={conn} status={status} rows={rows} download={download} />
      <div className="rail-foot">
        <button className="nav-item" aria-current={view === "settings" ? "page" : undefined} title="Settings"
          aria-label="Settings" onClick={() => onView("settings")}>
          <Gear size={20} weight={view === "settings" ? "fill" : "regular"} aria-hidden />
          <span className="nav-label">Settings</span>
        </button>
      </div>
    </div>
  );
}

/** The app mark: a strip of tape with three marks on it. */
export function Mark() {
  return (
    <svg className="mark" width="20" height="20" viewBox="0 0 20 20" aria-hidden>
      <path d="M2 10h16M6 6.5v7M10 4v12M14 6.5v7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none" />
    </svg>
  );
}

type Line = { tone: string; icon: Icon; state: string; detail: string };

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function statusLine(conn: Conn, status: Status | null, rows: Row[], download: string | null = null): Line {
  if (conn.state === "starting") return { tone: "muted", icon: HourglassMedium, state: "Starting the engine", detail: "" };
  if (conn.state === "stopped") return { tone: "failed", icon: WarningCircle, state: "Engine stopped", detail: "Details at the top" };
  const waiting = rows.filter((r) => r.stage === "waiting").length;
  const review = rows.filter((r) => r.stage === "review").length;
  const rest = [waiting ? `${waiting} waiting` : "", review ? `${review} to review` : ""].filter(Boolean).join(" · ");
  const gpu = status?.gpu;
  if (gpu?.paused) {
    return { tone: "paused", icon: Pause, state: gpu.manual ? "Paused by you" : `Paused${gpu.reason ? `: ${gpu.reason}` : ""}`,
      detail: rest || "Nothing waiting" };
  }
  const rec = rows.find((r) => r.stage === "recording");
  if (rec) {
    const title = rec.title === "Recording" ? "" : rec.title;           // a recording with no call has no title yet
    return { tone: "recording", icon: Circle, state: "Recording", detail: [title, rest].filter(Boolean).join(" · ") };
  }
  const job = status?.job;
  if (job?.file && job.step && job.step !== "failed") {
    return { tone: "working", icon: Waveform, state: STEPS[job.step] ?? job.step,
      detail: [parseInboxName(job.file).title, rest].filter(Boolean).join(" · ") };
  }
  const live = rows.find((r) => r.stage === "live");
  if (live) return { tone: "muted", icon: Circle, state: `Call detected · ${live.line}`, detail: rest };
  if (download) return { tone: "working", icon: DownloadSimple, state: download, detail: rest };
  if (review) return { tone: "failed", icon: WarningCircle, state: `${plural(review, "meeting")} to review`, detail: waiting ? `${waiting} waiting` : "" };
  return { tone: "muted", icon: Circle, state: waiting ? `${waiting} waiting` : "Nothing running",
    detail: waiting ? "" : "Every meeting has a note" };
}

function StatusArea({ conn, status, rows, download }: Pick<Props, "conn" | "status" | "rows" | "download">) {
  const line = statusLine(conn, status, rows, download);
  const Glyph = line.icon;
  return (
    <section className={`status tone-${line.tone}`} aria-label="Status" title={[line.state, line.detail].filter(Boolean).join("\n")}>
      <div className="status-state" aria-live="polite">
        <Glyph size={16} weight={line.tone === "muted" || line.tone === "working" ? "regular" : "fill"} aria-hidden />
        <span className="status-text">{line.state}</span>
      </div>
      {line.detail && <div className="status-detail">{line.detail}</div>}
    </section>
  );
}
