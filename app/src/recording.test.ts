import { describe, expect, it } from "vitest";
import { buildRows, type MeetingsData, type Session } from "./meetings";
import { micCheck, popupView, pushLevel, trackProblem, type PopupData, type Recording } from "./recording";
import { comboFrom, keyLabel } from "./Settings";
import type { Live, Meter, TrackStatus } from "./shell";

const NOW = new Date("2026-11-15T16:00:00+01:00");
const meet: Session = { id: "m1", app: "Google Meet", window_title: "Meet - abc-defg-hij", start: "2026-11-15T15:40:00+01:00",
  end: null, duration: 0, screenshots: [], client: null, call_title: null, skip: false };
const rec = (over: Partial<Recording> = {}): Recording => ({ state: "recording", folder: "/r/1", session: "m1", manual: false,
  started_at: "2026-11-15T15:48:00+01:00", ended_at: null, seconds: 0, marks: [], message: null, ...over });
const popup = (over: Partial<PopupData> = {}): PopupData => ({ session: meet, headline: "Google Meet · abc-defg-hij", live: true,
  remembered: null, recording: null, stage: null, auto_record: false, ...over });
const track = (over: Partial<TrackStatus> = {}): TrackStatus => ({ device: "Headset", fell_back: false, problem: null, rebuilds: 0, ...over });
const live = (mic: Partial<TrackStatus> = {}, system: Partial<TrackStatus> = {}): Live => ({ folder: "/r/1", seconds: 60, marks: [],
  mic: track(mic), mic_level: 0.5, system: track({ device: "Speakers", ...system }), system_level: 0 });

describe("call popup", () => {
  it("draws the state the UX plan names", () => {
    expect(popupView(null, NOW)).toEqual({ kind: "none" });
    expect(popupView(popup({ session: null, live: false }), NOW)).toEqual({ kind: "none" });
    expect(popupView(popup(), NOW)).toEqual({ kind: "detected" });
    expect(popupView(popup({ recording: rec() }), NOW)).toEqual({ kind: "recording", ask: false });
    // auto-record with no client known: the popup only asks who it is with
    expect(popupView(popup({ recording: rec({ state: "starting" }), auto_record: true }), NOW)).toEqual({ kind: "recording", ask: true });
    expect(popupView(popup({ recording: rec(), auto_record: true, remembered: "Acme" }), NOW)).toEqual({ kind: "recording", ask: false });
    expect(popupView(popup({ recording: rec({ state: "error", message: "busy" }) }), NOW)).toEqual({ kind: "failed", message: "busy" });
  });

  it("offers Record again for a minute after a manual stop while the call goes on", () => {
    const stopped = (ago: number) => rec({ state: "stopped", ended_at: new Date(NOW.getTime() - ago * 1000).toISOString() });
    expect(popupView(popup({ recording: stopped(10) }), NOW)).toEqual({ kind: "ended", recordAgain: true });
    expect(popupView(popup({ recording: stopped(61) }), NOW)).toEqual({ kind: "ended", recordAgain: false });
    expect(popupView(popup({ recording: stopped(10), live: false }), NOW)).toEqual({ kind: "ended", recordAgain: false });
  });

  it("an older recording of another meeting does not take over a new call", () => {
    expect(popupView(popup({ recording: rec({ session: "old", state: "stopped" }) }), NOW)).toEqual({ kind: "detected" });
  });

  it("says what is still captured when a track is in trouble", () => {
    expect(trackProblem(null, 0, 0)).toBeNull();
    expect(trackProblem(live(), 0, 0)).toBeNull();
    expect(trackProblem(live({ device: null }), 0, 0)).toBe("Microphone lost. Still recording the call.");
    expect(trackProblem(live({ fell_back: true, device: "Microphone Array" }), 0, 0)).toMatch(/Recording Microphone Array instead/);
    expect(trackProblem(live({}, { device: null }), 0, 0)).toBe("Call sound lost. Still recording you.");
    // the call track flat for 30 s while you talk: probably the wrong output device
    expect(trackProblem(live(), 31, 1)).toBe("Call: no sound?");
    expect(trackProblem(live(), 31, 20)).toBeNull();          // nobody talks: a quiet call is just quiet
  });
});

describe("settings", () => {
  const meter = (status: Partial<TrackStatus> = {}): Meter => ({ level: 0, status: track(status) });
  it("checks that the microphone hears you", () => {
    expect(micCheck(meter(), null, 0, 3000)).toBe("listening");
    expect(micCheck(meter(), null, 0, 9000)).toBe("silent");
    expect(micCheck(meter(), 2000, 0, 9000)).toBe("heard");
    expect(micCheck(meter({ device: null }), 2000, 0, 9000)).toBe("lost");
    expect(micCheck(meter({ fell_back: true }), null, 0, 100)).toBe("fell-back");
  });

  it("keeps the last levels for the strip", () => {
    expect(pushLevel([0.1, 0.2], 1.5, 3)).toEqual([0.1, 0.2, 1]);
    expect(pushLevel([0.1, 0.2, 0.3], -1, 3)).toEqual([0.2, 0.3, 0]);
  });

  it("reads and writes shortcuts", () => {
    const key = (code: string, mods: Partial<Record<"ctrlKey" | "altKey" | "shiftKey" | "metaKey", boolean>>) =>
      comboFrom({ ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, code, ...mods });
    expect(key("KeyR", { ctrlKey: true, altKey: true })).toBe("ctrl+alt+r");
    expect(key("Digit5", { ctrlKey: true, shiftKey: true })).toBe("ctrl+shift+5");
    expect(key("KeyR", {})).toBeNull();                                   // a plain letter would block typing
    expect(key("ControlLeft", { ctrlKey: true })).toBeNull();
    expect(keyLabel("ctrl+alt+r", "windows")).toBe("Ctrl+Alt+R");
    expect(keyLabel("ctrl+alt+r", "macos")).toBe("Control+Option+R");
    expect(keyLabel("", "linux")).toBe("");
  });
});

describe("meetings list while recording", () => {
  it("shows the recording in place of the detected call, and recordings waiting for transcription", () => {
    const data: MeetingsData = { calls: [], waiting: [], sessions: [{ ...meet, client: "Acme" }],
      recordings: [
        { folder: "/r/1", state: "recording", title: "Google Meet", session: "m1", started_at: "2026-11-15T15:48:00+01:00" },
        { folder: "/r/0", state: "transcribing", title: "Recording", session: null, started_at: "2026-11-15T09:00:00+01:00" },
      ] };
    const rows = buildRows(data, null, NOW);
    expect(rows.map((r) => [r.stage, r.client, r.line])).toEqual([
      ["recording", "Acme", "Recording"], ["working", null, "Transcribing"]]);
    expect(rows.some((r) => r.stage === "live")).toBe(false);
  });
});
