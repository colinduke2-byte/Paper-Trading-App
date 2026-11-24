// A series primitive (lightweight-charts v5 plugin API) that paints the chart drawings on the main pane.
// Coordinates are worked out on every redraw from the drawings' time and price anchors.
import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type { AutoscaleInfo, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesPrimitive, SeriesAttachedParameter, Time } from "lightweight-charts";
import { Drawing, fibLevels, rayEnd, XY } from "./drawings";

export type DrawingScene = {
  drawings: Drawing[];
  preview: Drawing | null;
  selected: string | null;
  toPx: (a: { t: number; p: number }) => XY | null;
  fmtPrice: (p: number) => string;
};

const COLOR = "#f0b429", SEL = "#4c9aff", FIB = ["#787b86", "#f23645", "#ff9800", "#4caf50", "#089981", "#2962ff", "#787b86"];
export const TEXT_FONT = "12px system-ui, -apple-system, Segoe UI, Roboto, sans-serif";

export function textBox(ctx: CanvasRenderingContext2D | null, text: string) {
  if (!ctx) return { w: text.length * 7 + 10, h: 18 };
  ctx.font = TEXT_FONT;
  return { w: ctx.measureText(text).width + 10, h: 18 };
}

class Renderer implements IPrimitivePaneRenderer {
  constructor(private scene: () => DrawingScene) {}
  draw(target: CanvasRenderingTarget2D) {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const s = this.scene();
      const all = s.preview ? [...s.drawings, s.preview] : s.drawings;
      for (const d of all) {
        try { paint(ctx, d, s, mediaSize.width, mediaSize.height, d.id === s.selected || d === s.preview); } catch {}
      }
    });
  }
}

function paint(ctx: CanvasRenderingContext2D, d: Drawing, s: DrawingScene, W: number, H: number, sel: boolean) {
  const pts = d.points.map(s.toPx);
  const [a, b] = pts;
  const color = sel ? SEL : COLOR;
  ctx.save();
  ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = sel ? 2 : 1.5;
  ctx.font = TEXT_FONT; ctx.textBaseline = "bottom";
  const line = (p: XY, q: XY) => { ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke(); };
  switch (d.type) {
    case "hline":
      if (a) { line({ x: 0, y: a.y }, { x: W, y: a.y }); ctx.fillText(s.fmtPrice(d.points[0].p), 4, a.y - 2); }
      break;
    case "vline":
      if (a) line({ x: a.x, y: 0 }, { x: a.x, y: H });
      break;
    case "trend":
      if (a && b) line(a, b);
      break;
    case "ray":
      if (a && b) line(a, rayEnd(a, b, W, H));
      break;
    case "rect":
      if (a && b) {
        const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y), w = Math.abs(a.x - b.x), h = Math.abs(a.y - b.y);
        ctx.globalAlpha = 0.12; ctx.fillRect(x, y, w, h); ctx.globalAlpha = 1; ctx.strokeRect(x, y, w, h);
      }
      break;
    case "fib":
      if (a && b) {
        const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x);
        fibLevels(d.points[0], d.points[1]).forEach((lv, i) => {
          const p = s.toPx({ t: d.points[0].t, p: lv.price });
          if (!p) return;
          ctx.strokeStyle = sel ? SEL : FIB[i]; ctx.fillStyle = ctx.strokeStyle;
          line({ x: x0, y: p.y }, { x: x1, y: p.y });
          ctx.fillText(`${(lv.level * 100).toFixed(1)}% ${s.fmtPrice(lv.price)}`, x0 + 3, p.y - 2);
        });
        ctx.setLineDash([4, 4]); ctx.strokeStyle = color; line(a, b); ctx.setLineDash([]);
      }
      break;
    case "text":
      if (a && d.text) {
        const box = textBox(ctx, d.text);
        ctx.fillStyle = "#12161cdd"; ctx.fillRect(a.x, a.y - box.h, box.w, box.h);
        ctx.strokeRect(a.x, a.y - box.h, box.w, box.h);
        ctx.fillStyle = sel ? "#ffffff" : "#f3d48a"; ctx.textBaseline = "middle";
        ctx.fillText(d.text, a.x + 5, a.y - box.h / 2);
      }
      break;
  }
  if (sel) {
    ctx.fillStyle = "#12161c"; ctx.strokeStyle = SEL; ctx.lineWidth = 2;
    const handles = d.type === "hline" || d.type === "vline" ? [] : pts;
    for (const p of handles) if (p) { ctx.beginPath(); ctx.arc(p.x, p.y, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
  }
  ctx.restore();
}

class View implements IPrimitivePaneView {
  private r: Renderer;
  constructor(scene: () => DrawingScene) { this.r = new Renderer(scene); }
  zOrder() { return "top" as const; }
  renderer() { return this.r; }
}

export class DrawingsPrimitive implements ISeriesPrimitive<Time> {
  private views: View[];
  private request: (() => void) | null = null;
  /** Prices the price scale should include (working orders, average cost), so their lines stay on screen. */
  extraPrices: () => number[] = () => [];
  constructor(private scene: () => DrawingScene, private onViewport: () => void) {
    this.views = [new View(scene)];
  }
  attached(p: SeriesAttachedParameter<Time>) { this.request = p.requestUpdate; }
  detached() { this.request = null; }
  /** Called by the chart whenever the view changes; also tells overlays (order handles) to reposition. */
  updateAllViews() { this.onViewport(); }
  paneViews() { return this.views; }
  autoscaleInfo(): AutoscaleInfo | null {
    const ps = this.extraPrices().filter((p) => isFinite(p) && p > 0);
    return ps.length ? { priceRange: { minValue: Math.min(...ps), maxValue: Math.max(...ps) } } : null;
  }
  redraw() { this.request?.(); }
}
