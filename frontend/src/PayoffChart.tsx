// Profit and loss at expiry against the underlying price, drawn as inline SVG from the legs (payoff.ts).
import { useEffect, useId, useRef, useState } from "react";
import { chartRange, curve, Leg } from "./payoff";
import { money, signed } from "./util";

export function PayoffChart({ legs, net, qty, spot, breakEvens }: { legs: Leg[]; net: number; qty: number; spot: number; breakEvens: number[] }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(560);
  const clip = useId().replace(/:/g, "");
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(240, Math.round(el.clientWidth))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const H = 210, padL = 58, padR = 12, padT = 22, padB = 34;
  const [lo, hi] = chartRange(legs, spot, breakEvens);
  const pts = curve(legs, net, qty, lo, hi, breakEvens);
  const ys = pts.map((p) => p[1]);
  let yMax = Math.max(0, ...ys), yMin = Math.min(0, ...ys);
  if (yMax - yMin < 1) { yMax += 1; yMin -= 1; }
  const yPad = (yMax - yMin) * 0.1;
  yMax += yPad; yMin -= yPad;
  const x = (v: number) => padL + ((v - lo) / (hi - lo)) * (width - padL - padR);
  const y = (v: number) => padT + ((yMax - v) / (yMax - yMin)) * (H - padT - padB);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  const area = `${line} L${x(hi).toFixed(1)},${y(0).toFixed(1)} L${x(lo).toFixed(1)},${y(0).toFixed(1)} Z`;
  const ticks = Array.from({ length: 5 }, (_, i) => lo + ((hi - lo) * i) / 4);
  const label = `Profit and loss at expiry for ${qty} unit${qty === 1 ? "" : "s"}. Break-even ${breakEvens.length ? breakEvens.map((b) => money(b)).join(" and ") : "none"}. Current price ${money(spot)}.`;

  return (
    <div ref={box} className="payoff">
      <svg width={width} height={H} role="img" aria-label={label} data-lo={lo} data-hi={hi}>
        <defs>
          <clipPath id={`${clip}-up`}><rect x={0} y={0} width={width} height={Math.max(0, y(0))} /></clipPath>
          <clipPath id={`${clip}-down`}><rect x={0} y={y(0)} width={width} height={Math.max(0, H - y(0))} /></clipPath>
        </defs>
        <path d={area} className="pf-gain" clipPath={`url(#${clip}-up)`} />
        <path d={area} className="pf-loss" clipPath={`url(#${clip}-down)`} />
        <line x1={padL} x2={width - padR} y1={y(0)} y2={y(0)} className="pf-zero" data-zero />
        <text x={padL - 6} y={y(0) + 4} className="pf-axis" textAnchor="end">0</text>
        <text x={padL - 6} y={padT + 4} className="pf-axis" textAnchor="end">{signed(yMax - yPad, 0)}</text>
        <text x={padL - 6} y={H - padB} className="pf-axis" textAnchor="end">{signed(yMin + yPad, 0)}</text>
        {ticks.map((t, i) => (
          <text key={i} x={x(t)} y={H - 12} className="pf-axis" textAnchor={i === 0 ? "start" : i === 4 ? "end" : "middle"}>{money(t)}</text>
        ))}
        <path d={line} className="pf-line" data-points={pts.map((p) => `${p[0]}:${p[1]}`).join(" ")} />
        <g data-spot={spot}>
          <line x1={x(spot)} x2={x(spot)} y1={padT - 6} y2={H - padB} className="pf-spot" />
          <text x={x(spot)} y={padT - 9} className="pf-spot-label" textAnchor="middle">Now {money(spot)}</text>
        </g>
        {breakEvens.map((b, i) => (
          <g key={b} data-breakeven={b.toFixed(2)}>
            <line x1={x(b)} x2={x(b)} y1={padT} y2={H - padB} className="pf-be" />
            <circle cx={x(b)} cy={y(0)} r={4} className="pf-be-dot" />
            <text x={x(b) + (breakEvens.length === 2 && i === 0 ? -5 : 5)} y={padT + 12} className="pf-be-label"
              textAnchor={breakEvens.length === 2 && i === 0 ? "end" : "start"}>Break-even {money(b)}</text>
          </g>
        ))}
      </svg>
      <p className="pf-legend muted">
        <span className="pf-key gain" /> profit <span className="pf-key loss" /> loss <span className="pf-key be" /> break-even <span className="pf-key spot" /> current price
      </p>
    </div>
  );
}
