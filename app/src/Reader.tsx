// The note reader: one renderer for every Markdown note (meetings, client profiles, parts, hand-written notes).
import { ArrowLeft, ArrowSquareOut, MagnifyingGlass, WarningCircle } from "@phosphor-icons/react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { useEffect, useState, type ReactNode } from "react";
import Markdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiError, inApp, type Api } from "./engine";
import { TYPE_LABEL, type NodeType } from "./graph";
import { MISSING_SCHEME, WIKI_SCHEME, crumbs, remarkCallouts, sections, slug, splitTarget, wikiToMarkdown, type Link, type Note } from "./note";

export type Loaded<T> = { state: "loading" } | { state: "ok"; value: T } | { state: "missing"; path: string } | { state: "error"; message: string };
export type Backlink = { path: string; title: string; type: string; client: string | null; date: string | null; contexts: string[] };

/** The note at `path`; a new `version` reloads it and keeps showing the old text meanwhile (no jump, no flash). */
export function useNote(api: Api | null, path: string | null, version: string | number = ""): Loaded<Note> {
  const [result, setResult] = useState<{ key: string; path: string | null; value: Loaded<Note> } | null>(null);
  const key = `${path}|${version}`;
  useEffect(() => {
    if (!api || !path) return;
    let live = true;
    api.get<Note>("/notes", { path }).then(
      (value) => live && setResult({ key, path, value: { state: "ok", value } }),
      (e) => live && setResult({ key, path, value: e instanceof ApiError && e.status === 404 ? { state: "missing", path }
        : { state: "error", message: String(e.message ?? e) } }));
    return () => { live = false; };
  }, [api, path, key]);
  if (result?.key === key) return result.value;
  if (result?.path === path && result.value.state === "ok") return result.value;
  return { state: "loading" };
}

export function openExternal(url: string) {
  if (inApp) openUrl(url).catch(() => {});
  else window.open(url, "_blank", "noopener");
}

function absolute(vault: string, rel: string) {
  return vault.includes("\\") ? `${vault}\\${rel.replaceAll("/", "\\")}` : `${vault}/${rel}`;
}

/** Obsidian's URL scheme (https://obsidian.md/help/uri); Obsidian finds the vault that holds the file. */
export function obsidianUrl(vault: string, path: string): string {
  return `obsidian://open?path=${encodeURIComponent(absolute(vault, path))}`;
}

export function ObsidianButton({ vault, path }: { vault: string; path: string }) {
  if (!vault) return null;
  return (
    <button className="button" onClick={() => openExternal(obsidianUrl(vault, path))} title="Ctrl+O">
      <ArrowSquareOut size={16} aria-hidden />Open in Obsidian
    </button>
  );
}

export function NoteState({ state }: { state: Loaded<unknown> }) {
  if (state.state === "loading") return <div className="skeleton detail-skeleton" aria-label="Loading note"><div /><div /><div /></div>;
  if (state.state === "missing") return <p className="detail-message">This note is no longer at <span className="path">{state.path}</span>.</p>;
  if (state.state === "error") return <p className="detail-message">The note could not be read: {state.message}</p>;
  return null;
}

function text(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(text).join("");
  return "";
}

export function NoteMarkdown({ body, links, onOpen }: { body: string; links?: Link[]; onOpen: (path: string) => void }) {
  const heading = (Tag: "h1" | "h2" | "h3") => ({ children }: { children?: ReactNode }) => <Tag id={slug(text(children))}>{children}</Tag>;
  const components: Components = {
    h1: heading("h1"), h2: heading("h2"), h3: heading("h3"),
    a: ({ href = "", children }) => href.startsWith(WIKI_SCHEME)
      ? <a href="#" className="wikilink" onClick={(e) => { e.preventDefault(); onOpen(decodeURI(href.slice(WIKI_SCHEME.length))); }}>{children}</a>
      : href.startsWith(MISSING_SCHEME)
        ? <span className="wikilink missing" title="This note does not exist">{children}</span>
        : <a href={href} onClick={(e) => { e.preventDefault(); openExternal(href); }}>{children}</a>,
  };
  return (
    <Markdown remarkPlugins={[remarkGfm, remarkCallouts]} components={components}
      urlTransform={(url) => (url.startsWith(WIKI_SCHEME) || url.startsWith(MISSING_SCHEME) ? url : defaultUrlTransform(url))}>
      {wikiToMarkdown(body, links)}
    </Markdown>
  );
}

/** `[[a/b|Alias]]` -> `Alias` in a quoted line. */
export function plainLinks(line: string): string {
  return line.replace(/!?\[\[([^\]|#]*)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]/g, (_a, t: string, alias?: string) => alias ?? t.split("/").at(-1) ?? t)
    .replace(/\*\*|__|`/g, "");
}

export function typeLabel(type: string): string {
  return TYPE_LABEL[type as NodeType] ?? TYPE_LABEL.other;
}

/** `version`: when the file last changed on disk while the app ran (0: not since the app started). */
type ReaderProps = { api: Api | null; target: string; vault: string; version: number; onOpen: (path: string) => void;
  onBack?: () => void; backLabel?: string; onSearch: (text: string) => void };

/** Any note, read-only: properties, the text with working links, what links here, and what it links to. */
export function NoteReader({ api, target, vault, version, onOpen, onBack, backLabel = "Back", onSearch }: ReaderProps) {
  const { path, anchor } = splitTarget(target);
  const note = useNote(api, path, version);
  const [back, setBack] = useState<{ key: string; list: Backlink[] } | null>(null);
  const { folders, name } = crumbs(path);
  const loaded = note.state === "ok";

  useEffect(() => {
    if (!api) return;
    let live = true;
    api.get<{ backlinks: Backlink[] }>("/backlinks", { path }).then((r) => live && setBack({ key: `${path}|${version}`, list: r.backlinks }), () => {});
    return () => { live = false; };
  }, [api, path, version]);
  useEffect(() => {
    if (!loaded) return;
    if (anchor) document.getElementById(anchor)?.scrollIntoView({ block: "start" });
  }, [loaded, anchor]);

  const fm = loaded ? note.value.frontmatter : {};
  // the header already shows the name: a first `# Name` that repeats it is dropped
  const body = loaded ? note.value.body.replace(new RegExp(`^\\s*# ${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\n`), "") : "";
  const links = loaded ? note.value.links : undefined;
  const type = fm.type ? typeLabel(String(fm.type)) : "Note";
  const part = fm.type === "transcript-chunk"
    ? `Part ${fm.chunk} of ${fm.of}${fm.time_start ? ` · ${fm.time_start}–${fm.time_end ?? ""}` : ""}` : "";
  const meta = [type, fm.client ? String(fm.client) : "", fm.date ? String(fm.date) : "", part].filter(Boolean).join(" · ");
  const backlinks = back?.key.startsWith(`${path}|`) ? back.list : null;
  const out = uniqueLinks(links ?? []);
  const toc = sections(body);

  return (
    <section className="detail note-reader" aria-label={name}>
      <header className="detail-head">
        {onBack && <button className="link-button back" onClick={onBack} title="Alt+Left"><ArrowLeft size={16} aria-hidden />{backLabel}</button>}
        {folders.length > 0 && <p className="crumbs">{folders.join(" / ")}</p>}
        <div className="head-row">
          <h1 className="head-title">{name}</h1>
          {loaded && <ObsidianButton vault={vault} path={path} />}
        </div>
        {loaded && (
          <p className="head-meta">{meta}
            {version > 0 && <span className="updated"> · Updated {new Date(version).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</span>}
          </p>
        )}
      </header>
      {note.state === "missing" ? (
        <div className="pending">
          <p className="detail-message flush">This note is no longer at <span className="path">{path}</span>.</p>
          <div className="actions">
            <button className="button" onClick={() => onSearch(name)}><MagnifyingGlass size={16} aria-hidden />Search for it</button>
            {onBack && <button className="button" onClick={onBack}><ArrowLeft size={16} aria-hidden />Back</button>}
          </div>
        </div>
      ) : note.state !== "ok" ? <div className="pending"><NoteState state={note} /></div> : (
        <div className="detail-body reader-body">
          <article className="reader">
            {Object.keys(fm).length > 0 && (
              <details className="properties">
                <summary>Properties</summary>
                <dl>{Object.entries(fm).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{Array.isArray(v) ? v.join(", ") : String(v)}</dd></div>)}</dl>
              </details>
            )}
            <NoteMarkdown body={body} links={links} onOpen={onOpen} />
          </article>
          <aside className="note-side" aria-label="Links">
            {toc.length > 1 && (
              <>
                <h2 className="side-title">On this page</h2>
                <ul className="side-list">{toc.map((s) => <li key={s.id}><a href={`#${s.id}`}><span>{s.title}</span>{s.count ? <span className="num">{s.count}</span> : null}</a></li>)}</ul>
              </>
            )}
            <h2 className="side-title">Linked from{backlinks?.length ? <span className="num">{backlinks.length}</span> : null}</h2>
            {backlinks === null ? <p className="side-note">Loading…</p>
              : backlinks.length === 0 ? <p className="side-note">No note links here.</p> : (
                <ul className="side-list">
                  {backlinks.map((b) => (
                    <li key={b.path}>
                      <button className="side-link" onClick={() => onOpen(b.path)}>
                        <span className="side-link-title">{b.title}</span>
                        {b.contexts[0] && <span className="side-quote">{plainLinks(b.contexts[0])}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            <h2 className="side-title">Links out{out.length ? <span className="num">{out.length}</span> : null}</h2>
            {out.length === 0 ? <p className="side-note">This note links to no other note.</p> : (
              <ul className="side-list">
                {out.map((l) => (
                  <li key={l.path ?? `missing:${l.target}`}>
                    {l.path ? <button className="side-link" onClick={() => onOpen(l.path!)}><span className="side-link-title">{crumbs(l.path).name}</span></button>
                      : <span className="side-missing" title="This note does not exist"><WarningCircle size={14} aria-hidden />
                        <span>{l.target.split("/").at(-1)}<span className="visually-hidden">, does not exist</span></span></span>}
                  </li>
                ))}
              </ul>
            )}
            <h2 className="side-title">File</h2>
            <p className="path">{path}</p>
          </aside>
        </div>
      )}
    </section>
  );
}

/** One entry per linked note (or missing name), attachments left out, in reading order. */
export function uniqueLinks(links: Link[]): Link[] {
  const seen = new Set<string>();
  return links.filter((l) => {
    if (l.attachment) return false;
    const key = l.path ?? `missing:${l.target.toLowerCase()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
