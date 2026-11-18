// Where the engine is and how to reach it. Inside the app the shell starts the engine and answers
// `engine_info`; in a plain browser (`npm run dev`) the address comes from `?port=&token=` in the URL.
import { invoke, isTauri } from "@tauri-apps/api/core";

export type EngineInfo =
  | { state: "ready"; port: number; token: string }
  | { state: "stopped"; error: string };

export const inApp = isTauri();

function browserInfo(): EngineInfo {
  const query = new URLSearchParams(location.search);
  const port = Number(query.get("port") ?? sessionStorage.getItem("engine.port"));
  const token = query.get("token") ?? sessionStorage.getItem("engine.token") ?? "";
  if (!port || !token) {
    return { state: "stopped", error: "Start `vault-ingest serve` and open this page with ?port=<port>&token=<token>." };
  }
  sessionStorage.setItem("engine.port", String(port));
  sessionStorage.setItem("engine.token", token);
  if (query.has("token")) history.replaceState(null, "", location.pathname);     // keep the token out of the address bar
  return { state: "ready", port, token };
}

/** Waits until the engine is ready or has stopped. */
export function engineInfo(): Promise<EngineInfo> {
  return inApp ? invoke<EngineInfo>("engine_info") : Promise.resolve(browserInfo());
}

export function restartEngine(): Promise<EngineInfo> {
  if (inApp) return invoke<EngineInfo>("restart_engine");
  location.reload();                         // shortcut: a browser cannot start the engine, it can only look again
  return new Promise(() => {});
}

export class Api {
  readonly base: string;
  constructor(private info: { port: number; token: string }) {
    this.base = `http://127.0.0.1:${info.port}`;
  }

  async get<T>(path: string, params?: Record<string, string>): Promise<T> {
    const url = new URL(path, this.base);
    for (const [k, v] of Object.entries(params ?? {})) url.searchParams.set(k, v);
    const response = await fetch(url, { headers: { Authorization: `Bearer ${this.info.token}` } });
    if (!response.ok) throw new ApiError(response.status, (await response.json().catch(() => ({}))).error ?? "");
    return response.json() as Promise<T>;
  }

  post<T>(path: string, body?: unknown): Promise<T> {
    return this.send<T>("POST", path, body);
  }

  patch<T>(path: string, body: unknown): Promise<T> {
    return this.send<T>("PATCH", path, body);
  }

  private async send<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await fetch(new URL(path, this.base), {
      method, body: body === undefined ? undefined : JSON.stringify(body),
      headers: { Authorization: `Bearer ${this.info.token}`, "Content-Type": "application/json" },
    });
    if (!response.ok) throw new ApiError(response.status, (await response.json().catch(() => ({}))).error ?? "");
    return response.json() as Promise<T>;
  }

  events(): EventSource {
    // EventSource cannot set headers; /events is the one route that takes the token in the URL
    return new EventSource(`${this.base}/events?token=${encodeURIComponent(this.info.token)}`);
  }
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message || `HTTP ${status}`);
  }
}
