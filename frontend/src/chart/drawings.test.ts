import { describe, expect, it } from "vitest";
import {
  describeDrawing, distToRay, distToSegment, Drawing, fibLevels, hitTest, logicalToTime, moveDrawing, parseDrawings, pick, rayEnd,
  serializeDrawings, timeToLogical, MAX_DRAWINGS,
} from "./drawings";

const close = (a: number | null, b: number) => expect(Math.abs((a as number) - b)).toBeLessThan(1e-9);

describe("serialisation", () => {
  const list: Drawing[] = [
    { id: "a", type: "hline", points: [{ t: 1700000000, p: 101.25 }] },
    { id: "b", type: "trend", points: [{ t: 1, p: 2 }, { t: 3, p: 4 }] },
    { id: "c", type: "text", points: [{ t: 5, p: 6 }], text: "Earnings" },
  ];
  it("round-trips", () => {
    expect(parseDrawings(serializeDrawings(list))).toEqual(list);
    expect(parseDrawings(JSON.parse(serializeDrawings(list)))).toEqual(list);
  });
  it("drops malformed entries", () => {
    const bad = [
      { id: "x", type: "circle", points: [{ t: 1, p: 1 }] },
      { id: "y", type: "trend", points: [{ t: 1, p: 1 }] }, // needs two points
      { id: "z", type: "hline", points: [{ t: 1, p: "9" }] },
      { id: "w", type: "hline", points: [{ t: 1, p: -2 }] },
      { id: "v", type: "text", points: [{ t: 1, p: 1 }], text: "   " },
      null, 7,
      { id: "ok", type: "vline", points: [{ t: 10, p: 1 }] },
    ];
    expect(parseDrawings(bad).map((d) => d.id)).toEqual(["ok"]);
    expect(parseDrawings("{not json")).toEqual([]);
    expect(parseDrawings({})).toEqual([]);
  });
  it("gives duplicate ids a new id and caps the count", () => {
    const dup = parseDrawings([list[0], list[0]]);
    expect(dup).toHaveLength(2);
    expect(dup[0].id).not.toBe(dup[1].id);
    const many = Array.from({ length: MAX_DRAWINGS + 5 }, (_, i) => ({ id: `n${i}`, type: "hline", points: [{ t: 0, p: 1 + i }] }));
    expect(parseDrawings(many)).toHaveLength(MAX_DRAWINGS);
  });
});

describe("time mapping", () => {
  const times = [1000, 1060, 1120, 1300]; // a gap before the last bar
  it("maps bar times to indexes and interpolates between them", () => {
    close(timeToLogical(times, 1060, 60), 1);
    close(timeToLogical(times, 1090, 60), 1.5);
    close(timeToLogical(times, 1210, 60), 2.5); // halfway across the gap
  });
  it("extrapolates beyond the data", () => {
    close(timeToLogical(times, 940, 60), -1);
    close(timeToLogical(times, 1420, 60), 5);
  });
  it("round-trips", () => {
    for (const t of [880, 1000, 1033, 1120, 1250, 1300, 1999]) close(logicalToTime(times, timeToLogical(times, t, 60)!, 60), t);
  });
  it("keeps a time in place when the timeframe changes", () => {
    // A line drawn at 10:30 on 5-minute bars sits halfway between the 10:00 and 11:00 bars on hourly bars.
    const t = 36000 + 1800;
    const five = Array.from({ length: 30 }, (_, i) => 36000 + i * 300);
    const hour = [32400, 36000, 39600, 43200];
    close(timeToLogical(five, t, 300), 6);
    close(timeToLogical(hour, t, 3600), 1.5);
  });
  it("handles empty data", () => {
    expect(timeToLogical([], 1, 60)).toBeNull();
    expect(logicalToTime([], 1, 60)).toBeNull();
  });
});

describe("geometry", () => {
  it("fibonacci levels run from the end of the move back to its start", () => {
    const lv = fibLevels({ t: 0, p: 100 }, { t: 1, p: 200 });
    expect(lv.map((l) => l.level)).toEqual([0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]);
    close(lv[0].price, 200); close(lv[1].price, 176.4); close(lv[3].price, 150); close(lv[4].price, 138.2); close(lv[6].price, 100);
  });
  it("distance to a segment and a ray", () => {
    const a = { x: 0, y: 0 }, b = { x: 10, y: 0 };
    close(distToSegment({ x: 5, y: 3 }, a, b), 3);
    close(distToSegment({ x: 14, y: 3 }, a, b), 5); // past the end: distance to b
    close(distToRay({ x: 14, y: 3 }, a, b), 3); // a ray continues past b
    close(distToRay({ x: -4, y: 3 }, a, b), 5); // but not behind a
  });
  it("ray end is on the box edge", () => {
    expect(rayEnd({ x: 0, y: 0 }, { x: 10, y: 5 }, 100, 100)).toEqual({ x: 100, y: 50 });
    expect(rayEnd({ x: 50, y: 50 }, { x: 50, y: 40 }, 100, 100)).toEqual({ x: 50, y: 0 });
  });
  it("hit tests each type", () => {
    const tol = 6;
    const H = { id: "h", type: "hline", points: [{ t: 0, p: 1 }] } as Drawing;
    expect(hitTest(H, [{ x: 0, y: 50 }], { x: 300, y: 54 }, tol, 400, 300)?.part).toBe("body");
    expect(hitTest(H, [{ x: 0, y: 50 }], { x: 300, y: 60 }, tol, 400, 300)).toBeNull();
    const T = { id: "t", type: "trend", points: [{ t: 0, p: 1 }, { t: 1, p: 2 }] } as Drawing;
    const tp = [{ x: 10, y: 10 }, { x: 110, y: 110 }];
    expect(hitTest(T, tp, { x: 111, y: 109 }, tol, 400, 300)?.part).toBe(1); // the end handle
    expect(hitTest(T, tp, { x: 60, y: 62 }, tol, 400, 300)?.part).toBe("body");
    expect(hitTest(T, tp, { x: 60, y: 90 }, tol, 400, 300)).toBeNull();
    const R = { id: "r", type: "rect", points: [{ t: 0, p: 1 }, { t: 1, p: 2 }] } as Drawing;
    expect(hitTest(R, tp, { x: 50, y: 50 }, tol, 400, 300)?.part).toBe("body");
    expect(hitTest(R, tp, { x: 200, y: 50 }, tol, 400, 300)).toBeNull();
    const V = { id: "v", type: "vline", points: [{ t: 0, p: 1 }] } as Drawing;
    expect(hitTest(V, [{ x: 80, y: 0 }], { x: 83, y: 200 }, tol, 400, 300)?.part).toBe("body");
    const X = { id: "x", type: "text", points: [{ t: 0, p: 1 }], text: "hi" } as Drawing;
    expect(hitTest(X, [{ x: 20, y: 40 }], { x: 40, y: 32 }, tol, 400, 300, { w: 40, h: 16 })?.part).toBe("body");
  });
  it("pick prefers the nearest hit", () => {
    const a = { id: "a", type: "hline", points: [{ t: 0, p: 1 }] } as Drawing;
    const b = { id: "b", type: "hline", points: [{ t: 0, p: 2 }] } as Drawing;
    const y: Record<string, number> = { a: 50, b: 54 };
    expect(pick([a, b], (d) => [{ x: 0, y: y[d.id] }], { x: 10, y: 53 }, 6, 400, 300)?.id).toBe("b");
    expect(pick([a, b], (d) => [{ x: 0, y: y[d.id] }], { x: 10, y: 51 }, 6, 400, 300)?.id).toBe("a");
    expect(pick([a, b], (d) => [{ x: 0, y: y[d.id] }], { x: 10, y: 200 }, 6, 400, 300)).toBeNull();
  });
  it("moves a whole drawing or one anchor through the pixel converter", () => {
    // A linear test converter: x pixels = time / 10, y pixels = 1000 - price * 10.
    const fromPx = (p: { x: number; y: number }) => ({ t: p.x * 10, p: (1000 - p.y) / 10 });
    const d = { id: "t", type: "trend", points: [{ t: 100, p: 50 }, { t: 200, p: 60 }] } as Drawing;
    const px = [{ x: 10, y: 500 }, { x: 20, y: 400 }];
    expect(moveDrawing(d, "body", px, 5, -10, fromPx).points).toEqual([{ t: 150, p: 51 }, { t: 250, p: 61 }]);
    expect(moveDrawing(d, 1, px, 5, -10, fromPx).points).toEqual([{ t: 100, p: 50 }, { t: 250, p: 61 }]);
    const h = { id: "h", type: "hline", points: [{ t: 7, p: 50 }] } as Drawing;
    expect(moveDrawing(h, "body", [{ x: 10, y: 500 }], 30, -20, fromPx).points).toEqual([{ t: 7, p: 52 }]);
  });
  it("describes drawings", () => {
    const f = (p: number) => p.toFixed(2);
    expect(describeDrawing({ id: "h", type: "hline", points: [{ t: 0, p: 101.25 }] }, String, f)).toBe("Horizontal line at 101.25");
    expect(describeDrawing({ id: "f", type: "fib", points: [{ t: 0, p: 1 }, { t: 1, p: 2 }] }, String, f)).toBe("Fibonacci retracement from 1.00 to 2.00");
  });
});
