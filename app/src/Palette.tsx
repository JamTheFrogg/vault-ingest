// The command palette (Ctrl/Cmd+K): search over every note, clients by name or alias, and commands.
import { ArrowRight, FileText, MagnifyingGlass, UsersThree } from "@phosphor-icons/react";
import type { Embedding } from "./ai";
import { useEffect, useRef, useState } from "react";
import type { ClientRow } from "./Clients";
import type { Api } from "./engine";
import { flat, groups, meaningAvailable, parse, type Command, type Hit, type Item } from "./palette";
import { typeLabel } from "./Reader";

type Props = { api: Api | null; initial: string; clients: ClientRow[]; commands: Command[]; recent: Hit[];
  onPick: (item: Item, inObsidian: boolean) => void; onClose: () => void };

export function Palette({ api, initial, clients, commands, recent, onPick, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [input, setInput] = useState(initial);
  const [found, setFound] = useState<{ q: string; meaning: boolean; hits: Hit[]; fts: boolean } | null>(null);
  const [active, setActive] = useState(0);
  const [semantic, setSemantic] = useState<Embedding | null>(null);
  const [meaning, setMeaning] = useState(false);
  const { mode, text } = parse(input);
  const wanted = mode === "all" && text ? text : "";
  const canMean = meaningAvailable(semantic);
  const byMeaning = meaning && canMean;

  useEffect(() => {
    const el = dialog.current;
    if (el && !el.open) el.showModal();                 // a modal dialog keeps focus inside and gives it back on close
  }, []);
  useEffect(() => {
    // whether search by meaning runs: an empty search answers with its state
    api?.get<{ semantic: Embedding }>("/search", { q: "" }).then((r) => setSemantic(r.semantic), () => setSemantic(null));
  }, [api]);
  useEffect(() => {
    if (!api || !wanted) return;
    let live = true;
    const timer = window.setTimeout(() => {
      const params: Record<string, string> = { q: wanted, limit: "40", ...(byMeaning ? { semantic: "1" } : {}) };
      api.get<{ results: Hit[]; fts: boolean; semantic: Embedding }>("/search", params)
        .then((r) => { if (!live) return; setFound({ q: wanted, meaning: byMeaning, hits: r.results, fts: r.fts }); setSemantic(r.semantic); },
          () => {
            if (!live) return;
            setFound({ q: wanted, meaning: byMeaning, hits: [], fts: true });
            if (byMeaning) setSemantic((e) => (e ? { ...e, state: "failed" } : e));    // went away: keyword search again
          });
    }, byMeaning ? 250 : 120);
    return () => { live = false; window.clearTimeout(timer); };
  }, [api, wanted, byMeaning]);

  const hits = found?.q === wanted && found.meaning === byMeaning ? found.hits : null;
  const list = groups(input, commands, clients, hits, recent);
  const items = flat(list);
  const at = Math.min(active, Math.max(0, items.length - 1));
  const searching = wanted !== "" && hits === null;

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length) setActive((at + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length);
    } else if (e.altKey && e.code === "KeyM" && canMean) {
      e.preventDefault();
      setMeaning((m) => !m);
      setActive(0);
    } else if (e.key === "Enter" && items[at]) {
      e.preventDefault();
      onPick(items[at], e.ctrlKey || e.metaKey);
    }
  };
  const starts = list.map((_, gi) => list.slice(0, gi).reduce((sum, g) => sum + g.items.length, 0));
  return (
    <dialog ref={dialog} className="palette" aria-label="Search and commands" onClose={onClose}
      onClick={(e) => { if (e.target === dialog.current) dialog.current?.close(); }}>
      <div className="palette-field">
        <MagnifyingGlass size={16} aria-hidden />
        <input autoFocus className="palette-input" value={input} placeholder="Search notes, clients and commands"
          role="combobox" aria-expanded="true" aria-controls="palette-list" aria-activedescendant={items[at] ? `pi-${at}` : undefined}
          onChange={(e) => { setInput(e.target.value); setActive(0); }} onKeyDown={onKey} spellCheck={false} />
        {canMean && mode === "all" && (
          <button className="meaning-toggle" aria-pressed={byMeaning} title="Alt+M"
            onClick={() => { setMeaning(!meaning); setActive(0); }}>
            By meaning</button>
        )}
      </div>
      <div className="palette-list" id="palette-list" role="listbox" aria-label="Results">
        {list.map((g, gi) => (
          <div key={g.label} role="group" aria-label={g.label}>
            <div className="palette-group">{g.label}</div>
            {g.items.map((item, k) => {
              const i = starts[gi] + k;
              return (
                <div key={`${g.label}${i}`} id={`pi-${i}`} role="option" aria-selected={i === at} className="palette-row"
                  onMouseMove={() => i !== at && setActive(i)} onClick={() => onPick(item, false)}>
                  <Row item={item} />
                </div>
              );
            })}
          </div>
        ))}
        {items.length === 0 && (
          <div className="palette-empty">
            {searching ? <span className="muted">Searching…</span> : text
              ? <>Nothing matches “{text}”.{mode !== "all" && <button className="link-button accent" onClick={() => setInput(text)}>Search everything</button>}</>
              : <span className="muted">Type to search your notes. <kbd>&gt;</kbd> commands only, <kbd>@</kbd> clients only.</span>}
          </div>
        )}
      </div>
      <div className="palette-foot">
        <span><kbd>Enter</kbd> open</span><span><kbd>Ctrl</kbd>+<kbd>Enter</kbd> open in Obsidian</span><span><kbd>Esc</kbd> close</span>
        {canMean && <span><kbd>Alt</kbd>+<kbd>M</kbd> by meaning</span>}
        {byMeaning && semantic && semantic.done < semantic.total &&
          <span className="muted">By meaning: {semantic.done.toLocaleString("en-US")} of {semantic.total.toLocaleString("en-US")} notes ready</span>}
        {found && !found.fts && <span className="muted">Plain search: this engine has no full-text index</span>}
      </div>
    </dialog>
  );
}

function Row({ item }: { item: Item }) {
  if (item.kind === "command") {
    return <><ArrowRight size={16} aria-hidden className="palette-icon" /><span className="palette-title">{item.label}</span>
      {item.hint && <kbd className="palette-hint">{item.hint}</kbd>}</>;
  }
  if (item.kind === "client") {
    return <><UsersThree size={16} aria-hidden className="palette-icon" /><span className="palette-title">{item.name}</span>
      <span className="palette-meta">{item.meta}</span></>;
  }
  const h = item.hit;
  const meta = [!h.type || h.type === "client" || h.type === "call" ? "" : typeLabel(h.type), h.type === "client" ? "" : h.client ?? "", h.date ?? ""].filter(Boolean).join(" · ");
  return (
    <>
      <FileText size={16} aria-hidden className="palette-icon" />
      <span className="palette-main">
        <span className="palette-line"><span className="palette-title">{h.title}</span>{meta && <span className="palette-meta">{meta}</span>}</span>
        {h.snippet.length > 0 && (
          <span className="palette-snippet">{h.snippet.map(([t, hit], i) => hit ? <mark key={i}>{t}</mark> : <span key={i}>{t}</span>)}</span>
        )}
      </span>
    </>
  );
}
