// The Meetings list: every stage from a detected call to a finished note, built from /meetings and /status.

export type Session = {
  id: string; app: string; window_title: string; start: string; end: string | null; duration: number;
  screenshots: { path: string; timestamp: string; offset: string }[];
  client: string | null; call_title: string | null; skip: boolean;
  marks?: { timestamp: string; offset: string }[];
};
/** A recording with no note yet: being recorded, or waiting for / in transcription. */
export type RecordingRow = { folder: string; state: "recording" | "waiting" | "transcribing"; title: string;
  session: string | null; started_at: string | null };
export type Call = {
  client: string; date: string; title: string; call_note: string; chunks: number | null;
  processed_at: string | null; generated_by: string | null; screenshots: number | null; session_id: string | null;
};
export type MeetingsData = { calls: Call[]; sessions: Session[]; waiting: string[]; recordings?: RecordingRow[] };
export type Job = { file: string; step: string; last_error: string };
export type GpuState = { paused: boolean; reason: string; manual: boolean; until: number | null; phase: string;
  inbox: number; recordings: number };
export type Status = { vault: string; mode: string; model: string; inbox: string[]; review: string[]; job: Job;
  gpu: GpuState; watcher: number | null };

export type Stage = "recording" | "live" | "waiting" | "working" | "review" | "ready";
export type Row = {
  key: string; stage: Stage; date: string; client: string | null; title: string;
  line: string;                 // counts, or what is happening
  seconds?: number;             // duration
  start?: string;               // live call: when it started
  call?: Call; session?: Session; file?: string; folder?: string;
};
export type Group = { label: string; rows: Row[] };

const INBOX_NAME = /^(.+?) - (\d{4}-\d{2}-\d{2}) - (.+)\.[^.]+$/;
export const STEPS: Record<string, string> = {
  transcribing: "Transcribing",
  summary: "Writing notes, step 1 of 3",
  evidence: "Writing notes, step 2 of 3",
  profile: "Writing notes, step 3 of 3",
  client: "Finding the client",
};
export const STEP_DETAIL: Record<string, string> = {
  transcribing: "Turning the audio into text",
  summary: "Reading the parts and writing the note",
  evidence: "Checking quotes against the transcript",
  profile: "Updating the client profile",
  client: "Working out which client this was with",
};

export function localDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** `<Client> - YYYY-MM-DD - <Title>.ext`, the name the pipeline and the phone import use. */
export function parseInboxName(name: string): { client: string | null; date: string | null; title: string } {
  const m = INBOX_NAME.exec(name);
  return m ? { client: m[1], date: m[2], title: m[3] } : { client: null, date: null, title: name.replace(/\.[^.]+$/, "") };
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function buildRows(data: MeetingsData, status: Status | null, now: Date): Row[] {
  const today = localDay(now);
  const sessions = new Map(data.sessions.map((s) => [s.id, s]));
  const rows: Row[] = [];

  const recorded = new Set<string>();
  for (const r of data.recordings ?? []) {
    const s = r.session ? sessions.get(r.session) : undefined;
    if (r.session) recorded.add(r.session);
    const start = r.started_at ?? s?.start ?? now.toISOString();
    rows.push({ key: `rec:${r.folder}`, stage: r.state === "recording" ? "recording" : r.state === "transcribing" ? "working" : "waiting",
      date: localDay(new Date(start)), client: s?.client ?? null, title: s?.call_title || s?.app || r.title,
      line: r.state === "recording" ? "Recording" : r.state === "transcribing" ? "Transcribing" : "Waiting to transcribe",
      start, session: s, folder: r.folder });
  }
  for (const s of data.sessions) {
    if (s.end !== null || recorded.has(s.id)) continue;      // ended calls appear through their note, or as a waiting file
    rows.push({ key: `live:${s.id}`, stage: "live", date: localDay(new Date(s.start)), client: s.client,
      title: s.call_title || s.app, line: s.app, start: s.start, session: s });
  }

  const job = status?.job;
  const gpu = status?.gpu;
  for (const file of data.waiting) {
    const { client, date, title } = parseInboxName(file);
    const active = job?.file === file && job.step && job.step !== "failed";
    const line = active ? STEPS[job.step] ?? job.step
      : gpu?.paused ? (gpu.manual ? "Paused by you" : `Paused${gpu.reason ? `: ${gpu.reason}` : ""}`) : "Waiting";
    rows.push({ key: `inbox:${file}`, stage: active ? "working" : "waiting", date: date ?? today, client, title,
      line, file });
  }

  for (const file of status?.review ?? []) {
    if (file.endsWith(".why.md")) continue;
    const { client, date, title } = parseInboxName(file);
    rows.push({ key: `review:${file}`, stage: "review", date: date ?? today, client, title, line: "Needs review", file });
  }

  for (const call of data.calls) {
    const session = call.session_id ? sessions.get(call.session_id) : undefined;
    const counts = [call.chunks ? plural(call.chunks, "part") : "", call.screenshots ? plural(call.screenshots, "screenshot") : ""];
    rows.push({ key: `call:${call.call_note}`, stage: "ready", date: call.date, client: call.client, title: call.title,
      line: counts.filter(Boolean).join(" · ") || "Ready", seconds: session?.duration, call, session });
  }
  return rows;
}

export function dayLabel(day: string, now: Date): string {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const [y, m, d] = day.split("-").map(Number);
  const then = new Date(y, m - 1, d);
  const days = Math.round((today.getTime() - then.getTime()) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "This week";
  return then.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

const ORDER: Record<Stage, number> = { recording: 0, live: 0, working: 1, waiting: 2, review: 3, ready: 4 };

/** Newest day first; inside a day, unfinished work above finished notes. */
export function groupRows(rows: Row[], now: Date): Group[] {
  const sorted = [...rows].sort((a, b) => b.date.localeCompare(a.date) || ORDER[a.stage] - ORDER[b.stage]
    || (b.call?.processed_at ?? "").localeCompare(a.call?.processed_at ?? ""));
  const groups: Group[] = [];
  for (const row of sorted) {
    const label = dayLabel(row.date, now);
    if (groups.at(-1)?.label !== label) groups.push({ label, rows: [] });
    groups.at(-1)!.rows.push(row);
  }
  return groups;
}

export type Filter = "all" | "working" | "review";
export function matches(row: Row, filter: Filter): boolean {
  if (filter === "working") return row.stage === "recording" || row.stage === "live" || row.stage === "working" || row.stage === "waiting";
  if (filter === "review") return row.stage === "review";
  return true;
}

export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

/** 42 min, 1 h 05 */
export function length(seconds: number): string {
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
}

export function hmsSeconds(text: unknown): number | null {
  if (typeof text !== "string" || !/^\d+(:\d{1,2}){0,2}$/.test(text.trim())) return null;
  return text.trim().split(":").map(Number).reduce((total, part) => total * 60 + part, 0);
}

const SWATCHES = ["#5FAFA3", "#7C9FD6", "#A891D6", "#CF87AE", "#D08A62", "#C2A85E", "#9DB06A", "#97A3AA"];

/** Clients get the eight colours in order of their names, repeating after eight. */
export function swatches(names: (string | null)[]): Map<string, string> {
  const sorted = [...new Set(names.filter((n): n is string => !!n))].sort((a, b) => a.localeCompare(b));
  return new Map(sorted.map((name, i) => [name, SWATCHES[i % SWATCHES.length]]));
}
