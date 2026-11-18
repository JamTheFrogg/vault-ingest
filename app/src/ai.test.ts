import { describe, expect, it } from "vitest";
import {
  applyProgress, bytes, downloadStatus, itemAction, itemLine, machine, missing, needed, overall, testLine, timeLeft,
  type Hardware, type Item,
} from "./ai";
import { statusLine } from "./Rail";

const item = (over: Partial<Item> = {}): Item => ({ id: "gemma-4-12b", kind: "chat", title: "Gemma 4 12B", note: "Best notes.",
  size: 6_895_000_000, licence: "apache-2.0", checked: "size", state: "missing", done: 0, total: 6_895_000_000, speed: 0,
  message: null, ...over });
const ITEMS: Item[] = [
  item({ id: "llama-win-cuda", kind: "llama-server", title: "llama.cpp", size: 710_849_313, total: 710_849_313, state: "done", done: 710_849_313 }),
  item(),
  item({ id: "gemma-4-e4b", title: "Gemma 4 E4B", size: 5_970_000_000, total: 5_970_000_000 }),
  item({ id: "whisper-large-v3", kind: "whisper", title: "Whisper large-v3", size: 3_093_552_740, total: 3_093_552_740 }),
  item({ id: "cuda-libs-win", kind: "gpu-libs", title: "NVIDIA libraries", size: 1_249_463_701, total: 1_249_463_701 }),
];
const hw: Hardware = { gpu: "nvidia", gpu_name: "NVIDIA GeForce RTX 3070", vram_mb: 8192, ram_mb: 32768, backend: "cuda",
  whisper_device: "cuda", recommend: "gemma-4-12b", reason: "", os: "windows" };

describe("AI downloads", () => {
  it("writes sizes and time left the way a download page does", () => {
    expect(bytes(6_720_000_000)).toBe("6.7 GB");
    expect(bytes(175_000_000)).toBe("175 MB");
    expect(timeLeft(1e9, 0)).toBeNull();                         // not promised before the speed is known
    expect(timeLeft(1e9, 10e6)).toBe("2 min");
    expect(timeLeft(10e9, 1e6)).toBe("2 h 47 min");
    expect(timeLeft(1e6, 10e6)).toBe("under a minute");
  });

  it("names each row's state and its one action", () => {
    expect(itemLine(item())).toBe("Best notes.");
    expect(itemAction(item())).toBe("download");
    const running = item({ state: "downloading", done: 2_400_000_000, speed: 12_000_000 });
    expect(itemLine(running)).toBe("2.4 GB of 6.9 GB · 12 MB/s · 6 min left");
    expect(itemAction(running)).toBe("pause");
    expect(itemLine(item({ state: "waiting", done: 1e9, message: "Waiting for the network" }))).toBe("Waiting for the network · 1.0 GB of 6.9 GB");
    expect(itemLine(item({ state: "paused", done: 1e9 }))).toBe("Paused at 1.0 GB of 6.9 GB");
    expect(itemAction(item({ state: "paused", done: 1e9 }))).toBe("resume");
    expect(itemAction(item({ state: "failed", message: "Needs 3.2 GB more free space." }))).toBe("retry");
    expect(itemLine(item({ state: "failed", message: "Needs 3.2 GB more free space." }))).toBe("Needs 3.2 GB more free space.");
    expect(itemAction(item({ state: "done" }))).toBeNull();
    expect(itemLine(item({ state: "downloading", done: 0, message: "The download was damaged. Downloading it again." })))
      .toBe("0 B of 6.9 GB · The download was damaged. Downloading it again.");
  });

  it("asks only for what the chosen engine needs", () => {
    expect(needed(ITEMS, "builtin", "gemma-4-12b").map((i) => i.id)).toEqual(["llama-win-cuda", "gemma-4-12b", "whisper-large-v3", "cuda-libs-win"]);
    expect(needed(ITEMS, "builtin", "gemma-4-e4b").map((i) => i.id)).toContain("gemma-4-e4b");
    expect(needed(ITEMS, "cloud", "gemma-4-12b").map((i) => i.id)).toEqual(["whisper-large-v3", "cuda-libs-win"]);
    expect(missing(needed(ITEMS, "builtin", "gemma-4-12b"))).toBe(6_895_000_000 + 3_093_552_740 + 1_249_463_701);
  });

  it("follows the progress events", () => {
    const next = applyProgress(ITEMS, { id: "gemma-4-12b", state: "downloading", done: 1e9, total: 6_895_000_000, speed: 5e6, message: null });
    expect(next[1].state).toBe("downloading");
    expect(next[2]).toBe(ITEMS[2]);
    const want = needed(next, "builtin", "gemma-4-12b");
    expect(overall(want).line).toMatch(/^Downloading AI 1.7 GB of 11.9 GB · \d+ min left$/);
    expect(overall(want.map((i) => ({ ...i, state: "done" as const }))).state).toBe("done");
    expect(overall([item({ state: "failed", message: "HTTP 404 for x" })]).line).toBe("Gemma 4 12B: HTTP 404 for x");
  });

  it("shows a running download in the status area", () => {
    expect(downloadStatus([{ id: "a", state: "done", done: 1, total: 1, speed: 0, message: null }])).toBeNull();
    const line = downloadStatus([{ id: "a", state: "downloading", done: 2.4e9, total: 11e9, speed: 1, message: null }]);
    expect(line).toBe("Downloading AI 2.4 GB of 11.0 GB");
    const ready = { state: "ready" as const, api: null as never };
    expect(statusLine(ready, null, [], line)).toMatchObject({ tone: "working", state: "Downloading AI 2.4 GB of 11.0 GB" });
  });
});

describe("hardware and the test", () => {
  it("describes this computer", () => {
    expect(machine(hw)).toBe("NVIDIA GeForce RTX 3070, 8 GB video memory, 32 GB RAM");
    expect(machine({ ...hw, gpu: "none", gpu_name: null, vram_mb: null, ram_mb: 16384 })).toBe("No graphics card found, 16 GB RAM");
  });

  it("says whether the model fits", () => {
    expect(testLine(null)).toBeNull();
    expect(testLine({ running: true, engine: "builtin" })?.tone).toBe("working");
    expect(testLine({ ok: true, engine: "builtin", tokens_per_sec: 24.13, seconds: 9.6, offloaded: [49, 49], fits: true }))
      .toEqual({ tone: "done", text: "It works (24.1 tokens a second, answered in 10 s). The whole model fits on the graphics card." });
    expect(testLine({ ok: true, engine: "builtin", tokens_per_sec: 6, seconds: 30, offloaded: [31, 49], fits: false })?.text)
      .toMatch(/Only 31 of 49 layers fit .* lighter model/);
    expect(testLine({ ok: true, engine: "builtin", tokens_per_sec: 3, seconds: 40, offloaded: null })?.text).toMatch(/processor\.$/);
    expect(testLine({ ok: true, engine: "cloud", seconds: 2 })).toEqual({ tone: "done", text: "It works (answered in 2 s)." });
    expect(testLine({ ok: false, message: "The built-in AI is not downloaded yet." })).toEqual({ tone: "failed", text: "The built-in AI is not downloaded yet." });
  });
});
