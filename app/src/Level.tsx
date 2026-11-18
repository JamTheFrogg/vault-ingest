import { HEARD } from "./recording";

/** The tape strip as a level meter: the last few seconds of level as ticks on a line, newest at the right. */
export function LevelStrip({ label, levels, size, warn = false }: { label: string; levels: number[]; size: number; warn?: boolean }) {
  const now = levels.at(-1) ?? 0;
  const bars = [...Array(Math.max(0, size - levels.length)).fill(0), ...levels];
  return (
    <span className={`level${warn ? " tone-paused" : ""}`}>
      <span className="level-label">{label}</span>
      <svg className="level-strip" width={size * 3} height="16" viewBox={`0 0 ${size * 3} 16`} role="meter"
        aria-label={`${label} level`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(now * 100)}>
        <line x1="0" x2={size * 3} y1="15.5" y2="15.5" className="level-line" />
        {bars.map((v, i) => v > 0.02 && (
          <rect key={i} x={i * 3} y={15 - Math.max(1, v * 14)} width="2" height={Math.max(1, v * 14)}
            className={v >= HEARD ? "level-bar" : "level-bar quiet"} />
        ))}
      </svg>
    </span>
  );
}
