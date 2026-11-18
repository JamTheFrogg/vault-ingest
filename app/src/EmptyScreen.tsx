import { DownloadSimple } from "@phosphor-icons/react";

const SCREENS = {
  import: { icon: DownloadSimple, title: "Import",
    text: "Recordings from your phone and from files will be brought in here. Until then, put files into your vault's 00_Inbox folder." },
};

export function EmptyScreen({ view }: { view: "import" }) {
  const { icon: Glyph, title, text } = SCREENS[view];
  return (
    <section className="empty-screen" aria-labelledby="empty-title">
      <Glyph size={32} aria-hidden className="empty-icon" />
      <h1 id="empty-title">{title}</h1>
      <p>{text}</p>
      <p className="muted">This screen comes in a later version.</p>
    </section>
  );
}
