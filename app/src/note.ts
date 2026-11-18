// Obsidian Markdown as the pipeline writes it, turned into what react-markdown can render.

export type Link = { target: string; heading: string | null; alias: string | null; embed: boolean; attachment: boolean;
  path: string | null };
export type Note = { path: string; frontmatter: Record<string, unknown>; body: string; links?: Link[] };
export type Section = { id: string; title: string; count: number | null };

export const WIKI_SCHEME = "vault:";
export const MISSING_SCHEME = "vault-missing:";
const ATTACHMENT = /\.(png|jpe?g|gif|webp|svg|bmp|pdf|mp3|mp4|m4a|wav|ogg|opus|webm|flac|txt|docx|xlsx|pptx|canvas|base|zip)$/i;  // index.py ATTACHMENT
const WIKI_LINK = /(!?)\[\[([^\]|#]*)(?:#([^\]|]*))?(?:\|([^\]]+))?\]\]/g;

/** `[[a/b#H|Alias]]` -> `[Alias](vault:a/b.md#s-h)`, using the engine's resolved `links` when given (short names,
 *  aliases); a link to a note that does not exist -> `vault-missing:`; `![[img.png]]` -> a line naming the image.
 *  Code spans are left alone. */
export function wikiToMarkdown(body: string, links?: Link[]): string {
  const resolved = new Map(links?.map((l) => [l.target.trim().toLowerCase(), l.path]));
  return body.split(/(```[\s\S]*?```|`[^`\n]*`)/).map((piece, i) => i % 2 ? piece : piece.replace(WIKI_LINK,
    (all, bang: string, target: string, heading?: string, alias?: string) => {
      const clean = target.trim().replace(/\\$/, "");
      if (!clean) return all;
      const name = clean.split("/").at(-1) ?? clean;
      const attachment = ATTACHMENT.test(clean);
      const label = (alias ?? name).replace(/[[\]]/g, "");
      if (attachment) return bang ? `*Image: ${name}*` : label;
      const known = resolved.get(clean.toLowerCase());
      const path = links ? known : /\.md$/i.test(clean) ? clean : `${clean}.md`;
      if (!path) return `[${label}](${MISSING_SCHEME}${encodeURIComponent(clean)})`;
      return `[${label}](${WIKI_SCHEME}${encodeURI(path).replace(/\(/g, "%28").replace(/\)/g, "%29")}${heading?.trim() ? `#${slug(heading.trim())}` : ""})`;
    })).join("");
}

/** `path#s-heading` -> its two halves. */
export function splitTarget(target: string): { path: string; anchor: string | null } {
  const at = target.indexOf("#");
  return at < 0 ? { path: target, anchor: null } : { path: target.slice(0, at), anchor: target.slice(at + 1) || null };
}

/** `Clients/Acme/Calls/x.md` -> ["Clients", "Acme", "Calls"] and the note's name. */
export function crumbs(path: string): { folders: string[]; name: string } {
  const parts = path.split("/");
  return { folders: parts.slice(0, -1), name: parts.at(-1)!.replace(/\.md$/i, "") };
}

/** The `## heading` section's text, without the heading; null when the note has none. */
export function section(body: string, heading: string): string | null {
  const lines = body.split("\n");
  const start = lines.findIndex((l) => l.trim().toLowerCase() === `## ${heading.toLowerCase()}`);
  if (start < 0) return null;
  const end = lines.findIndex((l, i) => i > start && /^#{1,2} \S/.test(l));
  return lines.slice(start + 1, end < 0 ? undefined : end).join("\n").trim();
}

/** The body without the named `##` sections. */
export function withoutSections(body: string, headings: string[]): string {
  const drop = new Set(headings.map((h) => h.toLowerCase()));
  let skipping = false;
  return body.split("\n").filter((line) => {
    const h = /^## (.+)$/.exec(line);
    if (h) skipping = drop.has(h[1].trim().toLowerCase());
    else if (/^# \S/.test(line)) skipping = false;
    return !skipping;
  }).join("\n");
}

export function slug(text: string): string {
  return "s-" + text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "");
}

/** The note's `##` sections with the number of top-level list items under each (parts, decisions, ...). */
export function sections(body: string): Section[] {
  const out: Section[] = [];
  let inFence = false;
  for (const line of body.split("\n")) {
    if (line.startsWith("```")) inFence = !inFence;
    if (inFence) continue;
    const heading = /^## (.+)$/.exec(line);
    if (heading) {
      out.push({ id: slug(heading[1].trim()), title: heading[1].trim(), count: 0 });
    } else if (out.length && /^[-*] (?!_None\._)/.test(line)) {
      out.at(-1)!.count! += 1;
    }
  }
  return out.map((s) => ({ ...s, count: s.count || null }));
}

/** The header shows the title, client and date, so the note's own `# Title` and `**Client:**` line are dropped. */
export function bodyForDetail(body: string): string {
  return body.replace(/^\s*# [^\n]*\n+/, "").replace(/^\*\*Client:\*\*[^\n]*\n+/, "");
}

/** Part notes linked from the Transcript section, in order. */
export function partLinks(body: string): string[] {
  const transcript = /^## Transcript\n([\s\S]*?)(?=^## |$(?![\s\S]))/m.exec(body)?.[1] ?? "";
  return [...transcript.matchAll(/\[\[([^\]|#]+)/g)].map((m) => (m[1].endsWith(".md") ? m[1] : `${m[1]}.md`));
}

type MdNode = { type: string; value?: string; children?: MdNode[]; data?: Record<string, unknown> };

/** remark plugin: `> [!note]- Title` callouts become <details>; `+` or no sign opens them. */
export function remarkCallouts() {
  return (tree: MdNode) => {
    const walk = (node: MdNode) => {
      for (const child of node.children ?? []) {
        if (child.type === "blockquote") callout(child);
        walk(child);
      }
    };
    walk(tree);
  };
}

function callout(quote: MdNode): void {
  const first = quote.children?.[0];
  const text = first?.type === "paragraph" ? first.children?.[0] : undefined;
  const m = text?.type === "text" ? /^\[!(\w+)\]([-+]?)[ \t]*([^\n]*)\n?/.exec(text.value ?? "") : null;
  if (!m || !first || !text) return;
  const [all, kind, fold, title] = m;
  text.value = text.value!.slice(all.length);
  const rest = first.children!.slice(text.value ? 0 : 1);
  const summary: MdNode = { type: "paragraph", children: [{ type: "text", value: title || kind[0].toUpperCase() + kind.slice(1) }],
    data: { hName: "summary" } };
  quote.children = [summary, ...(rest.length ? [{ ...first, children: rest }] : []), ...quote.children!.slice(1)];
  quote.data = { hName: "details", hProperties: { className: `callout callout-${kind.toLowerCase()}`, open: fold !== "-" } };
}
