// The pointer layer used in draw mode. It covers only the main pane's plot area, so in draw mode the chart never
// sees these pointer events and a tap-drag draws instead of panning. Leave draw mode to pan and zoom again.
import { useEffect, useRef } from "react";
import { Anchor, Drawing, DrawingType, moveDrawing, newDrawingId, pick, POINTS, XY } from "./drawings";
import { fromPx, Gen, plotRect, toPx } from "./engine";
import { textBox } from "./primitive";
import { useViewport } from "./parts";

let measure: CanvasRenderingContext2D | null | undefined;
const measureCtx = () => (measure === undefined ? (measure = document.createElement("canvas").getContext("2d")) : measure);

export type Tool = "select" | DrawingType;
export type SceneState = { drawings: Drawing[]; draft: Drawing[] | null; preview: Drawing | null; selected: string | null };

export function DrawLayer({ g, scene, tool, setTool, drawings, setDrawings, setSelected, onText, onMenu }: {
  g: Gen; scene: React.MutableRefObject<SceneState>; tool: Tool; setTool: (t: Tool) => void;
  drawings: Drawing[]; setDrawings: (d: Drawing[]) => void; setSelected: (id: string | null) => void;
  onText: (at: XY, anchor: Anchor) => void; onMenu: (at: XY) => void;
}) {
  useViewport(g);
  const ref = useRef<HTMLDivElement>(null);
  const act = useRef<null | {
    kind: "move"; id: string; part: "body" | number; start: XY; startPx: (XY | null)[]; orig: Drawing; moved: boolean;
  } | { kind: "create"; type: DrawingType; start: XY; a: Anchor; twoClick: boolean }>(null);
  const press = useRef<{ timer: any; at: XY } | null>(null);
  const rect = plotRect(g);
  // Switching tool (or Escape, which selects the pointer tool) abandons a half-drawn shape.
  useEffect(() => () => { act.current = null; scene.current.preview = null; if (g.alive) g.primitive.redraw(); }, [tool, g]);

  const local = (e: React.PointerEvent | React.MouseEvent): XY => {
    const r = ref.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const redraw = () => g.alive && g.primitive.redraw();
  const touchTol = (e: React.PointerEvent) => (e.pointerType === "touch" ? 14 : 7);
  const ctx = measureCtx();
  const pxOf = (d: Drawing) => d.points.map((a) => toPx(g, a));

  const finish = (d: Drawing) => {
    scene.current.preview = null;
    act.current = null;
    setDrawings([...drawings, d]);
    setSelected(d.id);
    setTool("select");
  };

  const down = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    ref.current!.setPointerCapture(e.pointerId);
    const p = local(e);
    if (e.pointerType === "touch") {
      clearTimeout(press.current?.timer);
      press.current = { at: p, timer: setTimeout(() => { press.current = null; cancel(); onMenu(p); }, 600) };
    }
    const cur = act.current;
    if (cur?.kind === "create" && cur.twoClick) return; // the second click of a two-click line; finished on pointer up
    if (tool === "select") {
      const hit = pick(drawings, pxOf, p, touchTol(e), rect.width, rect.height, (d) => textBox(ctx, d.text || ""));
      setSelected(hit?.id ?? null);
      scene.current.selected = hit?.id ?? null;
      if (hit) {
        const orig = drawings.find((d) => d.id === hit.id)!;
        act.current = { kind: "move", id: hit.id, part: hit.part, start: p, startPx: pxOf(orig), orig, moved: false };
      }
      redraw();
      return;
    }
    const a = fromPx(g, p);
    if (!a) return;
    act.current = { kind: "create", type: tool, start: p, a, twoClick: false };
    scene.current.preview = { id: "preview", type: tool, points: POINTS[tool] === 2 ? [a, a] : [a], text: tool === "text" ? "Note" : undefined };
    redraw();
  };

  const move = (e: React.PointerEvent<HTMLDivElement>) => {
    const p = local(e);
    if (press.current && Math.hypot(p.x - press.current.at.x, p.y - press.current.at.y) > 10) { clearTimeout(press.current.timer); press.current = null; }
    const cur = act.current;
    if (!cur) return;
    if (cur.kind === "move") {
      const dx = p.x - cur.start.x, dy = p.y - cur.start.y;
      if (!cur.moved && Math.hypot(dx, dy) < 3) return;
      cur.moved = true;
      const moved = moveDrawing(cur.orig, cur.part, cur.startPx, dx, dy, (q) => fromPx(g, q));
      scene.current.draft = drawings.map((d) => (d.id === cur.id ? moved : d));
      redraw();
      return;
    }
    const b = fromPx(g, p);
    if (!b || !scene.current.preview) return;
    scene.current.preview = { ...scene.current.preview, points: POINTS[cur.type] === 2 ? [cur.a, b] : [b] };
    redraw();
  };

  const up = (e: React.PointerEvent<HTMLDivElement>) => {
    if (press.current) { clearTimeout(press.current.timer); press.current = null; }
    const p = local(e);
    const cur = act.current;
    if (!cur) return;
    if (cur.kind === "move") {
      act.current = null;
      const draft = scene.current.draft;
      scene.current.draft = null;
      if (cur.moved && draft) setDrawings(draft);
      redraw();
      return;
    }
    const b = fromPx(g, p);
    if (!b) return;
    if (POINTS[cur.type] === 1) {
      if (cur.type === "text") { scene.current.preview = null; act.current = null; redraw(); onText(p, b); return; }
      finish({ id: newDrawingId(), type: cur.type, points: [b] });
      return;
    }
    const far = Math.hypot(p.x - cur.start.x, p.y - cur.start.y) > 6;
    if (!far && !cur.twoClick) { cur.twoClick = true; return; } // a tap: the next tap sets the second point
    finish({ id: newDrawingId(), type: cur.type, points: [cur.a, b] });
  };

  const cancel = () => {
    act.current = null;
    scene.current.preview = null;
    scene.current.draft = null;
    redraw();
  };

  return (
    <div ref={ref} className={"ct-draw-layer" + (tool === "select" ? " select" : "")} data-testid="draw-layer"
      style={{ left: rect.left, top: rect.top, width: rect.width, height: rect.height }}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel}
      onContextMenu={(e) => { e.preventDefault(); cancel(); onMenu(local(e)); }} />
  );
}
