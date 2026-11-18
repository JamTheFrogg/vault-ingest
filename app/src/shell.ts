// What only the desktop shell can do: the recorder's live state, the microphones, the popup window.
// In a plain browser (`npm run dev`) these answer "nothing there", so every screen still renders.
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { inApp } from "./engine";

export type MicChoice = { kind: "default" } | { kind: "communications" } | { kind: "device"; id: string; name: string };
export type Device = { id: string; name: string; default: boolean; communications: boolean };
export type TrackStatus = { device: string | null; fell_back: boolean; problem: string | null; rebuilds: number };
export type Live = { folder: string; seconds: number; marks: number[]; mic: TrackStatus; mic_level: number;
  system: TrackStatus; system_level: number };
export type Meter = { level: number; status: TrackStatus };
export type Platform = { os: string; content_protection: boolean; shortcuts: boolean };

function call<T>(command: string, args: Record<string, unknown> | undefined, fallback: T): Promise<T> {
  return inApp ? invoke<T>(command, args) : Promise.resolve(fallback);
}

export const recorderLive = () => call<Live | null>("recorder_live", undefined, null);
export const micDevices = () => call<{ devices: Device[]; communications: boolean }>("mic_devices", undefined,
  { devices: [], communications: false });
export const micLevels = () => call<[Meter, Meter] | null>("mic_levels", undefined, null);
export const micPreview = (choice: MicChoice | null) => call<void>("mic_preview", { choice }, undefined);
export const micSelect = (choice: MicChoice) => call<void>("mic_select", { choice }, undefined);
export const popupLayout = (height: number, keyboard: boolean) => call<void>("popup_layout", { height, keyboard }, undefined);
export const popupHide = () => call<void>("popup_hide", undefined, undefined);
export const openMain = (meeting: string | null) => call<void>("open_main", { meeting }, undefined);
export const platform = () => call<Platform>("platform", undefined,
  { os: /Win/.test(navigator.userAgent) ? "windows" : /Mac/.test(navigator.userAgent) ? "macos" : "linux",
    content_protection: !/Linux/.test(navigator.userAgent), shortcuts: true });

/** A shell event (`popup-show`, `recorder`, `open-meeting`); returns the unsubscribe function. */
export function onShell<T>(event: string, handler: (payload: T) => void): () => void {
  if (!inApp) return () => {};
  const off = listen<T>(event, (e) => handler(e.payload));
  return () => { off.then((stop) => stop()); };
}

/** Which window this page is: the call popup or the main window. */
export function isPopup(): boolean {
  const label = (window as { __TAURI_INTERNALS__?: { metadata?: { currentWindow?: { label?: string } } } })
    .__TAURI_INTERNALS__?.metadata?.currentWindow?.label;
  return label ? label === "popup" : new URLSearchParams(location.search).get("view") === "popup";
}
