// The call popup and the recording rows: which state to draw, from what the engine and the shell report.
import type { Session } from "./meetings";
import type { Live, Meter } from "./shell";

export type Recording = {
  state: "starting" | "recording" | "stopped" | "error"; folder: string; session: string | null; manual: boolean;
  started_at: string; ended_at: string | null; seconds: number; marks: number[]; message: string | null;
};
/** Where the note of a stopped recording is: the queue's state, then the pipeline's. */
export type Stage = "queued" | "waiting" | "transcribing" | "writing" | "review" | "failed" | "ready" | "saved";
export type PopupData = {
  session: Session | null; headline: string; live: boolean; remembered: string | null;
  recording: Recording | null; stage: Stage | null; auto_record: boolean;
};

export type PopupView =
  | { kind: "none" }                                       // reopened with no call and nothing recorded
  | { kind: "detected" }                                   // 1. a call, not recording
  | { kind: "recording"; ask: boolean }                    // 2. auto-recording, client unknown / 3. recording
  | { kind: "ended"; recordAgain: boolean }                // 5. saved
  | { kind: "failed"; message: string };                   // the recorder could not start

export const RECORD_AGAIN_SECONDS = 60;
export const CLOSE_AFTER_END_SECONDS = 20;
export const FOLD_AFTER_SECONDS = 10;
const CALL_SILENT_SECONDS = 30;

export function recordingNow(rec: Recording | null | undefined): rec is Recording & { state: "starting" | "recording" } {
  return !!rec && (rec.state === "starting" || rec.state === "recording");
}

export function clientOf(data: PopupData): string | null {
  return data.session?.client ?? data.remembered ?? null;
}

export function popupView(data: PopupData | null, now: Date): PopupView {
  if (!data) return { kind: "none" };
  const rec = data.recording;
  const ours = rec && (!data.session || !rec.session || rec.session === data.session.id) ? rec : null;
  if (recordingNow(ours)) return { kind: "recording", ask: data.auto_record && !clientOf(data) };
  if (ours?.state === "error") return { kind: "failed", message: ours.message ?? "The recorder failed." };
  if (ours?.state === "stopped") {
    const since = ours.ended_at ? (now.getTime() - new Date(ours.ended_at).getTime()) / 1000 : Infinity;
    return { kind: "ended", recordAgain: data.live && since < RECORD_AGAIN_SECONDS };
  }
  return data.live || data.session ? { kind: "detected" } : { kind: "none" };
}

export const STAGE_LINE: Record<Stage, string> = {
  queued: "Waiting to transcribe",
  saved: "Saved",
  waiting: "Waiting to transcribe",
  transcribing: "Transcribing",
  writing: "Writing notes",
  review: "Needs review",
  failed: "Could not transcribe. Details in the main window",
  ready: "Note ready",
};

/** What the first line says when a track is in trouble while recording (popup state 6). */
export function trackProblem(live: Live | null, callSilentFor: number, micSilentFor: number): string | null {
  if (!live) return null;
  if (!live.mic.device) return "Microphone lost. Still recording the call.";
  if (live.mic.fell_back) return `Your microphone is not connected. Recording ${live.mic.device} instead.`;
  if (!live.system.device) return "Call sound lost. Still recording you.";
  if (callSilentFor >= CALL_SILENT_SECONDS && micSilentFor < 5) return "Call: no sound?";
  return null;
}

/** Above this the meter counts as sound (0..1 is -60..0 dBFS: 0.25 is about -45 dB). */
export const HEARD = 0.25;

export type MicCheck = "listening" | "heard" | "silent" | "lost" | "fell-back";
/** The "say something" check in Settings: heard once is enough; 8 s of nothing means a muted or wrong device. */
export function micCheck(meter: Meter | null, heardAt: number | null, openedAt: number, now: number): MicCheck {
  if (meter && !meter.status.device) return "lost";
  if (meter?.status.fell_back) return "fell-back";
  if (heardAt !== null) return "heard";
  return now - openedAt > 8000 ? "silent" : "listening";
}

/** Keeps the last `size` levels: the strip the meter draws, newest at the right. */
export function pushLevel(history: number[], level: number, size: number): number[] {
  const next = [...history, Math.max(0, Math.min(1, level))];
  return next.length > size ? next.slice(next.length - size) : next;
}

export function elapsed(rec: Recording, live: Live | null, now: Date): number {
  if (live && live.folder === rec.folder) return live.seconds;
  return Math.max(0, (now.getTime() - new Date(rec.started_at).getTime()) / 1000);
}
