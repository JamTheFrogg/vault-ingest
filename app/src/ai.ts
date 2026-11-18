// The AI engine screen's logic (plan/03-ux-shape.md 3.8 "AI engine", 5 "Step 3", 6 "Model download"):
// what the engine reports in GET /settings/ai, and the words the screen shows for it.

export type Engine = "builtin" | "lmstudio" | "ollama" | "cloud";
export type ItemState = "missing" | "queued" | "downloading" | "waiting" | "verifying" | "installing" | "done" | "paused" | "failed";
export type Item = {
  id: string; kind: "llama-server" | "chat" | "whisper" | "gpu-libs" | "embedding"; title: string; note: string | null;
  size: number; licence: string; checked: "sha256" | "size"; optional?: boolean;
  state: ItemState; done: number; total: number; speed: number; message: string | null;
};
export type Hardware = {
  gpu: "nvidia" | "apple" | "none"; gpu_name: string | null; vram_mb: number | null; ram_mb: number | null;
  backend: string; whisper_device: string; recommend: string; reason: string; os: string;
};
export type TestResult = {
  running?: boolean; ok?: boolean; engine?: Engine; model?: string | null; tokens_per_sec?: number | null;
  ttft?: number | null; offloaded?: [number, number] | null; fits?: boolean | null; vram?: [number, number] | null;
  seconds?: number | null; message?: string | null;
};
export type AiSettings = {
  engine: Engine; model: string; address: string | null; key_hint: string | null; hardware: Hardware; items: Item[];
  default_chat: string; light_chat: string; models_dir: string; free_bytes: number; test: TestResult | null; pending: boolean;
  embedding?: Embedding;
};
/** Search by meaning in the engine: off (not downloaded), starting, indexing, waiting, ready or failed. */
export type Embedding = { state: "off" | "starting" | "indexing" | "waiting" | "ready" | "failed"; done: number; total: number; message: string | null };
export type Progress = Pick<Item, "id" | "state" | "done" | "total" | "speed" | "message">;

export const ADDRESSES: Record<"lmstudio" | "ollama", string> = {
  lmstudio: "http://localhost:1234", ollama: "http://localhost:11434",
};
export const BUSY: ItemState[] = ["queued", "downloading", "waiting", "verifying", "installing"];

/** 6720000000 -> "6.7 GB"; sizes are decimal, as download pages show them. */
export function bytes(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} KB`;
  return `${n} B`;
}

/** "2 h 5 min", "6 min", "under a minute"; null while the speed is not known yet (no promise in advance). */
export function timeLeft(remaining: number, speed: number): string | null {
  if (speed <= 0) return null;
  const minutes = Math.round(remaining / speed / 60);
  if (minutes < 1) return "under a minute";
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
}

/** An event from /events merged into the row it belongs to. */
export function applyProgress(items: Item[], p: Progress): Item[] {
  return items.map((i) => (i.id === p.id ? { ...i, ...p } : i));
}

/** What the row says about its download. */
export function itemLine(i: Item): string {
  const of = `${bytes(i.done)} of ${bytes(i.total || i.size)}`;
  switch (i.state) {
    case "done": return "Installed";
    case "queued": return "Waiting for the download before it";
    case "downloading": {
      const left = timeLeft((i.total || i.size) - i.done, i.speed);
      return [of, i.speed > 0 ? `${bytes(i.speed)}/s` : "", left ? `${left} left` : "", i.message ?? ""].filter(Boolean).join(" · ");
    }
    case "waiting": return `${i.message ?? "Waiting for the network"} · ${of}`;
    case "verifying": return "Checking the file";
    case "installing": return "Unpacking";
    case "paused": return `Paused at ${of}`;
    case "failed": return i.message ?? "The download failed.";
    default: return i.note ?? "";
  }
}

/** The one action each row offers. */
export function itemAction(i: Item): "download" | "pause" | "resume" | "retry" | null {
  if (i.state === "done") return null;
  if (BUSY.includes(i.state)) return "pause";
  if (i.state === "failed") return "retry";
  return i.done > 0 ? "resume" : "download";
}

/** The downloads the chosen engine needs: speech to text always, the server and one model only for built-in.
 * Search by meaning (and the processor build it runs on) is optional and never part of it. */
export function needed(items: Item[], engine: Engine, model: string): Item[] {
  return items.filter((i) => !i.optional && (i.kind === "whisper" || i.kind === "gpu-libs"
    || (engine === "builtin" && (i.kind === "llama-server" || i.id === model))));
}

/** One line about search by meaning under the models; null while it is off. */
export function meaningLine(e: Embedding | undefined): { tone: string; text: string } | null {
  if (!e || e.state === "off") return null;
  const n = (x: number) => x.toLocaleString("en-US");
  switch (e.state) {
    case "starting": return { tone: "working", text: "Search by meaning is starting…" };
    case "failed": return { tone: "failed", text: e.message ?? "Search by meaning stopped. It is tried again in a minute." };
    case "waiting": return { tone: "paused", text: `${e.message ?? "Waiting."} ${n(e.done)} of ${n(e.total)} notes are ready.` };
    case "indexing": return { tone: "working", text: `Preparing notes for search by meaning: ${n(e.done)} of ${n(e.total)}` };
    default: return { tone: "done", text: `Search by meaning is ready for all ${n(e.total)} notes.` };
  }
}

export function missing(items: Item[]): number {
  return items.filter((i) => i.state !== "done").reduce((sum, i) => sum + Math.max(0, i.size - i.done), 0);
}

/** One line over several downloads, for the status area and the onboarding step. */
export function overall(items: Item[]): { state: "idle" | "busy" | "done" | "failed"; line: string } {
  if (!items.length) return { state: "idle", line: "" };
  const total = items.reduce((s, i) => s + (i.total || i.size), 0);
  const done = items.reduce((s, i) => s + (i.state === "done" ? i.total || i.size : i.done), 0);
  const failed = items.find((i) => i.state === "failed");
  if (failed) return { state: "failed", line: `${failed.title}: ${failed.message ?? "the download failed"}` };
  if (items.every((i) => i.state === "done")) return { state: "done", line: "Everything is downloaded" };
  const busy = items.filter((i) => BUSY.includes(i.state));
  if (!busy.length) return { state: "idle", line: `${bytes(total - done)} to download` };
  const speed = busy.reduce((s, i) => s + i.speed, 0);
  const left = timeLeft(total - done, speed);
  const waiting = busy.some((i) => i.state === "waiting");
  return { state: "busy", line: [`Downloading AI ${bytes(done)} of ${bytes(total)}`, waiting ? "waiting for the network" : left ? `${left} left` : ""].filter(Boolean).join(" · ") };
}

/** "NVIDIA GeForce RTX 3070, 8 GB video memory, 32 GB RAM" */
export function machine(hw: Hardware): string {
  const ram = hw.ram_mb ? `${Math.round(hw.ram_mb / 1024)} GB RAM` : "";
  if (hw.gpu === "nvidia") return [hw.gpu_name, `${Math.round((hw.vram_mb ?? 0) / 1024)} GB video memory`, ram].filter(Boolean).join(", ");
  if (hw.gpu === "apple") return ["Apple Silicon", ram].filter(Boolean).join(", ");
  return ["No graphics card found", ram].filter(Boolean).join(", ");
}

/** What "Test my setup" found, in a sentence; tone for the colour. */
export function testLine(t: TestResult | null): { tone: string; text: string } | null {
  if (!t) return null;
  if (t.running) return { tone: "working", text: t.engine === "builtin" ? "Loading the model and asking it one question…" : "Asking one question…" };
  if (!t.ok) return { tone: "failed", text: t.message ?? "The test failed." };
  const facts = [t.tokens_per_sec ? `${t.tokens_per_sec.toFixed(1)} tokens a second` : "",
    t.seconds != null ? `answered in ${Math.round(t.seconds)} s` : ""].filter(Boolean);
  const head = `It works${facts.length ? ` (${facts.join(", ")})` : ""}`;
  if (t.engine !== "builtin") return { tone: "done", text: `${head}.` };
  if (!t.offloaded) return { tone: "done", text: `${head}. It runs on the processor.` };
  const [n, m] = t.offloaded;
  if (t.fits) return { tone: "done", text: `${head}. The whole model fits on the graphics card.` };
  if (n === m) return { tone: "paused", text: `${head}. The model is on the graphics card, but the card is nearly full; it may slow down when other apps need it.` };
  return { tone: "paused", text: `${head}. Only ${n} of ${m} layers fit on the graphics card, so it is slower. The lighter model may suit this computer better.` };
}

/** "Downloading AI 2.4 of 11.0 GB" for the status area, from the latest event of each download; null when none runs. */
export function downloadStatus(progress: Progress[]): string | null {
  const busy = progress.filter((p) => BUSY.includes(p.state));
  if (!busy.length) return null;
  const done = busy.reduce((s, p) => s + p.done, 0);
  const total = busy.reduce((s, p) => s + p.total, 0);
  return busy.some((p) => p.state === "waiting") ? "AI download waiting for the network" : `Downloading AI ${bytes(done)} of ${bytes(total)}`;
}
