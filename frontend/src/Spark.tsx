// A small inline line chart (chart data: floats are fine here). Green when the last point is above the first.
export function Spark({ points, width = 64, height = 18, label }: { points?: number[] | null; width?: number; height?: number; label?: string }) {
  if (!points || points.length < 2) return <span className="muted">–</span>;
  const lo = Math.min(...points), hi = Math.max(...points);
  const span = hi - lo || 1;
  const step = width / (points.length - 1);
  const d = points.map((p, i) => `${(i * step).toFixed(1)},${(height - 1 - ((p - lo) / span) * (height - 2)).toFixed(1)}`).join(" ");
  const up = points[points.length - 1] >= points[0];
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img"
      aria-label={label || `Intraday line, ${up ? "up" : "down"}`}>
      <polyline points={d} fill="none" stroke={up ? "var(--up)" : "var(--down)"} strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  );
}

// Position of the last price in a low-high range, drawn as a bar with a marker.
export function RangeBar({ pos, low, high, label }: { pos?: number | null; low?: number | null; high?: number | null; label: string }) {
  if (pos == null || low == null || high == null) return <span className="muted">–</span>;
  const p = Math.max(0, Math.min(100, pos));
  return (
    <span className="rangebar" role="img" aria-label={`${label}: ${p.toFixed(0)}% of the way from ${low.toFixed(2)} to ${high.toFixed(2)}`}
      title={`${low.toFixed(2)} – ${high.toFixed(2)}`}>
      <i style={{ left: `${p}%` }} />
    </span>
  );
}
