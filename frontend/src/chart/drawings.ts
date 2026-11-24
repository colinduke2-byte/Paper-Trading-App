// Chart drawings: types, serialisation and geometry. Pure functions only (no chart or DOM access), so they can be
// unit tested. A drawing is anchored to real time (UTC seconds) and price, never to pixels or bar indexes, so it
// stays in place across zoom, pan and timeframe changes.

export type DrawingType = "trend" | "ray" | "hline" | "vline" | "rect" | "fib" | "text";
export type Anchor = { t: number; p: number };
export type Drawing = { id: string; type: DrawingType; points: Anchor[]; text?: string };
export type XY = { x: number; y: number };

export const DRAWING_NAMES: Record<DrawingType, string> = {
  trend: "Trend line", ray: "Ray", hline: "Horizontal line", vline: "Vertical line", rect: "Rectangle",
  fib: "Fibonacci retracement", text: "Text note",
};
/** How many anchor points each type has. */
export const POINTS: Record<DrawingType, 1 | 2> = { trend: 2, ray: 2, hline: 1, vline: 1, rect: 2, fib: 2, text: 1 };
export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
export const MAX_DRAWINGS = 200;
export const MAX_TEXT = 200;

const num = (v: unknown): v is number => typeof v === "number" && isFinite(v);

// ---- serialisation -------------------------------------------------------------------------------------

export function serializeDrawings(list: Drawing[]): string {
  return JSON.stringify(list.map((d) => ({ id: d.id, type: d.type, points: d.points.map((a) => ({ t: a.t, p: a.p })), ...(d.text != null ? { text: d.text } : {}) })));
}

/** Parse stored drawings (a JSON string or an already parsed value), dropping anything malformed. */
export function parseDrawings(raw: unknown): Drawing[] {
  let v = raw;
  if (typeof raw === "string") { try { v = JSON.parse(raw); } catch { return []; } }
  if (!Array.isArray(v)) return [];
  const out: Drawing[] = [];
  const seen = new Set<string>();
  for (const d of v) {
    if (!d || typeof d !== "object") continue;
    const type = (d as any).type as DrawingType;
    if (!(type in POINTS)) continue;
    const pts = (d as any).points;
    if (!Array.isArray(pts) || pts.length !== POINTS[type]) continue;
    if (!pts.every((a: any) => a && num(a.t) && num(a.p) && a.p > 0)) continue;
    let id = typeof (d as any).id === "string" && (d as any).id ? String((d as any).id).slice(0, 40) : newDrawingId();
    if (seen.has(id)) id = newDrawingId();
    seen.add(id);
    const dr: Drawing = { id, type, points: pts.map((a: any) => ({ t: a.t, p: a.p })) };
    if (type === "text") {
      const text = typeof (d as any).text === "string" ? (d as any).text.slice(0, MAX_TEXT) : "";
      if (!text.trim()) continue;
      dr.text = text;
    }
    out.push(dr);
    if (out.length >= MAX_DRAWINGS) break;
  }
  return out;
}

let seq = 0;
export function newDrawingId(): string {
  return `d${Date.now().toString(36)}${(seq++).toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

// ---- time axis mapping ---------------------------------------------------------------------------------
// The chart's horizontal axis is a sequence of bars (logical index 0, 1, 2, ...), not linear time. These map a
// UTC time to a fractional logical index on the current bars and back. Between bars the position is interpolated;
// before the first or after the last bar it is extrapolated with `step` seconds per bar.

export function timeToLogical(times: number[], t: number, step: number): number | null {
  const n = times.length;
  if (!n || !num(t)) return null;
  if (t <= times[0]) return (t - times[0]) / step;
  if (t >= times[n - 1]) return n - 1 + (t - times[n - 1]) / step;
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (times[mid] <= t) lo = mid; else hi = mid; }
  return lo + (t - times[lo]) / (times[hi] - times[lo]);
}

export function logicalToTime(times: number[], logical: number, step: number): number | null {
  const n = times.length;
  if (!n || !num(logical)) return null;
  if (logical <= 0) return times[0] + logical * step;
  if (logical >= n - 1) return times[n - 1] + (logical - (n - 1)) * step;
  const i = Math.floor(logical);
  return times[i] + (logical - i) * (times[i + 1] - times[i]);
}

// ---- geometry ------------------------------------------------------------------------------------------

export function fibLevels(a: Anchor, b: Anchor): { level: number; price: number }[] {
  // 0 % at the second point (the end of the move), 100 % back at the first.
  return FIB_LEVELS.map((level) => ({ level, price: b.p - (b.p - a.p) * level }));
}

export function distToSegment(p: XY, a: XY, b: XY): number {
  const dx = b.x - a.x, dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const u = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
  return Math.hypot(p.x - (a.x + u * dx), p.y - (a.y + u * dy));
}

export function distToRay(p: XY, a: XY, b: XY): number {
  const dx = b.x - a.x, dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const u = len2 === 0 ? 0 : Math.max(0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2);
  return Math.hypot(p.x - (a.x + u * dx), p.y - (a.y + u * dy));
}

/** Where a ray from a through b leaves the box [0, width] x [0, height]. */
export function rayEnd(a: XY, b: XY, width: number, height: number): XY {
  const dx = b.x - a.x, dy = b.y - a.y;
  if (dx === 0 && dy === 0) return b;
  let t = Infinity;
  if (dx > 0) t = Math.min(t, (width - a.x) / dx); else if (dx < 0) t = Math.min(t, -a.x / dx);
  if (dy > 0) t = Math.min(t, (height - a.y) / dy); else if (dy < 0) t = Math.min(t, -a.y / dy);
  if (!isFinite(t) || t < 1) t = 1;
  return { x: a.x + dx * t, y: a.y + dy * t };
}

export type TextBox = { w: number; h: number };
export type Hit = { part: "body" | number; distance: number };

/**
 * Hit test one drawing whose anchors have been converted to pixels (`pts`). Returns the part under the pointer:
 * an anchor index (to drag that point) or "body" (to move the whole drawing), or null.
 */
export function hitTest(d: Drawing, pts: (XY | null)[], p: XY, tol: number, width: number, height: number, text?: TextBox): Hit | null {
  const [a, b] = pts;
  const handles = POINTS[d.type] === 2 || d.type === "text" ? pts : [];
  for (let i = 0; i < handles.length; i++) {
    const h = handles[i];
    if (h && Math.hypot(p.x - h.x, p.y - h.y) <= tol + 2) return { part: i, distance: Math.hypot(p.x - h.x, p.y - h.y) };
  }
  let dist = Infinity;
  switch (d.type) {
    case "hline": if (a) dist = Math.abs(p.y - a.y); break;
    case "vline": if (a) dist = Math.abs(p.x - a.x); break;
    case "trend": if (a && b) dist = distToSegment(p, a, b); break;
    case "ray": if (a && b) dist = distToRay(p, a, b); break;
    case "rect": if (a && b) {
      const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x), y0 = Math.min(a.y, b.y), y1 = Math.max(a.y, b.y);
      dist = p.x >= x0 - tol && p.x <= x1 + tol && p.y >= y0 - tol && p.y <= y1 + tol ? 0 : Infinity;
      break;
    }
    case "fib": if (a && b) {
      const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x), y0 = Math.min(a.y, b.y), y1 = Math.max(a.y, b.y);
      dist = p.x >= x0 - tol && p.x <= x1 + tol && p.y >= y0 - tol && p.y <= y1 + tol ? 0 : Infinity;
      break;
    }
    case "text": if (a && text) dist = p.x >= a.x - tol && p.x <= a.x + text.w + tol && p.y >= a.y - text.h - tol && p.y <= a.y + tol ? 0 : Infinity; break;
  }
  if (d.type === "hline" && (p.x < 0 || p.x > width)) dist = Infinity;
  if (d.type === "vline" && (p.y < 0 || p.y > height)) dist = Infinity;
  return dist <= tol ? { part: "body", distance: dist } : null;
}

/** The drawing hit nearest to the pointer, preferring anchors, then the most recently drawn. */
export function pick(list: Drawing[], toPx: (d: Drawing) => (XY | null)[], p: XY, tol: number, width: number, height: number,
  textBox?: (d: Drawing) => TextBox): { id: string; part: "body" | number } | null {
  let best: { id: string; part: "body" | number; score: number } | null = null;
  for (let i = list.length - 1; i >= 0; i--) {
    const d = list[i];
    const h = hitTest(d, toPx(d), p, tol, width, height, textBox?.(d));
    if (!h) continue;
    const score = (h.part === "body" ? tol : 0) + h.distance;
    if (!best || score < best.score) best = { id: d.id, part: h.part, score };
  }
  return best && { id: best.id, part: best.part };
}

/** Replace one anchor (part = index) or translate every anchor (part = "body") using a pixel-to-anchor converter. */
export function moveDrawing(d: Drawing, part: "body" | number, startPx: (XY | null)[], dx: number, dy: number,
  fromPx: (p: XY) => Anchor | null): Drawing {
  const points = d.points.map((a, i) => {
    if (part !== "body" && part !== i) return a;
    const s = startPx[i];
    if (!s) return a;
    const moved = fromPx({ x: s.x + dx, y: s.y + dy });
    if (!moved) return a;
    // A horizontal line keeps no time and a vertical line no price; keep the unused coordinate stable.
    if (d.type === "hline") return { t: a.t, p: moved.p };
    if (d.type === "vline") return { t: moved.t, p: a.p };
    return moved;
  });
  return { ...d, points };
}

/** A short description, such as "Horizontal line at 101.25". */
export function describeDrawing(d: Drawing, fmtTime: (t: number) => string, fmtPrice: (p: number) => string): string {
  const [a, b] = d.points;
  switch (d.type) {
    case "hline": return `Horizontal line at ${fmtPrice(a.p)}`;
    case "vline": return `Vertical line at ${fmtTime(a.t)}`;
    case "text": return `Text note "${d.text}" at ${fmtPrice(a.p)}`;
    default: return `${DRAWING_NAMES[d.type]} from ${fmtPrice(a.p)} to ${fmtPrice(b.p)}`;
  }
}
