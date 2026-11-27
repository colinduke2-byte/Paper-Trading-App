// Strategy builder: multi-leg option orders (spreads, straddles, condors, covered calls...) priced and
// placed as one all-or-none order. A wide dialog on desktop, a full-screen sheet on a phone.
import { useEffect, useRef, useState } from "react";
import { api, money, qty as fmtQty, signed, usd } from "./util";
import { usePoll } from "./Market";
import {
  analyse, describeLeg, Leg, legFromSymbol, legSymbol, midNet, naturalNet, Px, recognise, shortDate, Side, strikeText,
} from "./payoff";
import { defaultExpiries, templateLegs, TEMPLATES, TemplateId } from "./strategy";
import { PayoffChart } from "./PayoffChart";
import { isoDay, netText, TIF_LABELS } from "./orderText";
import "./strategy.css";

export type BuilderInit = { symbol: string; legs?: { symbol: string; side: Side; ratio: number }[]; qty?: number; title?: string; nonce: number };

// Polls the option chain of each expiry the legs use.
function useChains(underlying: string, expiries: string[]): Record<string, any> {
  const [chains, setChains] = useState<Record<string, any>>({});
  const key = [...new Set(expiries.filter(Boolean))].sort().join(",");
  useEffect(() => {
    if (!key) return;
    let alive = true;
    const load = () => Promise.all(key.split(",").map((e) =>
      api(`/options/chain/${underlying}?expiry=${e}`).then((c) => [e, c] as const).catch(() => [e, null] as const)))
      .then((rs) => { if (alive) setChains((prev) => ({ ...prev, ...Object.fromEntries(rs.filter((r) => r[1])) })); });
    load();
    const t = setInterval(load, 3000);
    return () => { alive = false; clearInterval(t); };
  }, [underlying, key]);
  return chains;
}

const sourceLabel = (s: string | undefined) => (!s ? "–" : s === "model" ? "model" : s);

export function StrategyBuilder({ init, accountId, stockQuote, feed, onClose, onPlaced }: {
  init: BuilderInit; accountId: string; stockQuote: any; feed: string; onClose: () => void; onPlaced: (msg: string) => void;
}) {
  const u = init.symbol;
  const prefilled = !!init.legs?.length;
  const [exps] = usePoll<{ date: string; dte: number }[]>(`/options/expirations/${u}`, 0);
  const [template, setTemplate] = useState<TemplateId>(prefilled ? "custom" : "bull_call");
  const [near, setNear] = useState("");
  const [far, setFar] = useState("");
  const [legs, setLegs] = useState<Leg[]>(() => (init.legs || []).map((l) => legFromSymbol(l.symbol, l.side, l.ratio)));
  const [needsTemplate, setNeedsTemplate] = useState(!prefilled);
  const [qty, setQty] = useState(String(init.qty || 1));
  const [type, setType] = useState("limit");
  const [limit, setLimit] = useState("");
  const [effect, setEffect] = useState<"debit" | "credit">("debit");
  const [priceTouched, setPriceTouched] = useState(false);
  const [tif, setTif] = useState("day");
  const [gtd, setGtd] = useState(isoDay(7));
  const [preview, setPreview] = useState<any>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const first = useRef<HTMLSelectElement>(null);
  const result = useRef<HTMLDivElement>(null);
  const tpl = TEMPLATES.find((t) => t.id === template)!;

  useEffect(() => { first.current?.focus(); }, []);
  // Bring the preview (or the rejection) into view: on a phone it is below the legs.
  useEffect(() => { if (preview || error) result.current?.scrollIntoView?.({ block: "nearest" }); }, [preview, error]);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  // Default expiries once the list arrives (and the prefilled legs' expiry when closing a strategy).
  useEffect(() => {
    if (!exps || near) return;
    const [n, f] = defaultExpiries(exps);
    const opt = legs.find((l) => l.kind !== "S");
    setNear(opt ? opt.expiry : n);
    setFar(f);
  }, [exps]);

  const chains = useChains(u, [near, tpl.twoExpiries ? far : "", ...legs.map((l) => l.expiry)]);
  const spot: number | undefined = chains[near]?.spot ?? stockQuote?.last;
  const strikesOf = (e: string): number[] => (chains[e]?.rows || []).map((r: any) => r.strike);

  // Build the template's legs once the chain of its expiry has loaded.
  useEffect(() => {
    if (!needsTemplate || !near || !chains[near] || (tpl.twoExpiries && !far)) return;
    setLegs(templateLegs(template, strikesOf(near), chains[near].spot, near, far));
    setNeedsTemplate(false); setPriceTouched(false);
  }, [needsTemplate, near, far, template, chains[near]]);

  const quoteOf = (l: Leg): { px: Px; source?: string } => {
    if (l.kind === "S") return { px: stockQuote ? { bid: stockQuote.bid, ask: stockQuote.ask } : null, source: feed };
    const ch = chains[l.expiry];
    const row = ch?.rows?.find((r: any) => Math.abs(r.strike - l.strike) < 1e-6);
    const o = row?.[l.kind === "C" ? "call" : "put"];
    return { px: o ? { bid: o.bid, ask: o.ask } : null, source: o?.source || ch?.source };
  };
  const quotes = legs.map(quoteOf);
  const natural = naturalNet(legs, quotes.map((q) => q.px));
  const mid = midNet(legs, quotes.map((q) => q.px));

  // The limit follows the middle price until it is typed in.
  useEffect(() => {
    if (priceTouched || mid == null) return;
    setLimit(Math.abs(mid).toFixed(2));
    setEffect(mid < 0 ? "credit" : "debit");
  }, [mid, priceTouched]);

  const edit = (fn: () => void) => { fn(); setPreview(null); setError(""); };
  const pickTemplate = (id: TemplateId) => edit(() => { setTemplate(id); setNeedsTemplate(true); });
  const setLeg = (i: number, patch: Partial<Leg>) => edit(() => setLegs(legs.map((l, j) => (j === i ? { ...l, ...patch } : l))));
  const changeKind = (i: number, kind: Leg["kind"]) => {
    const l = legs[i];
    if (kind === "S") return setLeg(i, { kind, ratio: 100, strike: 0, expiry: "" });
    const strikes = strikesOf(l.expiry || near);
    const strike = l.kind === "S" ? strikes.reduce((a, b) => (Math.abs(b - (spot || 0)) < Math.abs(a - (spot || 0)) ? b : a), strikes[0] ?? 0) : l.strike;
    setLeg(i, { kind, ratio: l.kind === "S" ? 1 : l.ratio, strike, expiry: l.expiry || near });
  };
  const addLeg = () => {
    const strikes = strikesOf(near);
    const used = new Set(legs.filter((l) => l.kind === "C" && l.expiry === near).map((l) => l.strike));
    const around = [...strikes].sort((a, b) => Math.abs(a - (spot || 0)) - Math.abs(b - (spot || 0)));
    const strike = around.find((k) => !used.has(k)) ?? strikes[0] ?? 0;
    edit(() => setLegs([...legs, { side: "buy", kind: "C", strike, expiry: near, ratio: 1 }]));
  };

  const name = recognise(legs) || (legs.length ? "Custom strategy" : "No legs yet");
  const hasStock = legs.some((l) => l.kind === "S");
  const signedLimit = type === "limit" && limit !== "" && isFinite(Number(limit)) ? (effect === "credit" ? -1 : 1) * Number(limit) : null;
  const previewNet = preview ? (preview.price_effect === "credit" ? -preview.net_price : preview.net_price) : null;
  const chartNet = previewNet ?? signedLimit ?? natural ?? mid;
  const units = Math.max(1, Math.floor(Number(qty)) || 1);
  const shape = chartNet != null && legs.length ? analyse(legs, chartNet, units) : null;

  const body = () => ({
    legs: legs.map((l) => ({ symbol: legSymbol(u, l), side: l.side, ratio: l.ratio })),
    qty, type, tif,
    limit_price: type === "limit" ? limit : null,
    price_effect: type === "limit" ? effect : null,
    good_till_date: tif === "gtd" ? gtd : null,
  });

  const review = async () => {
    setBusy(true); setError("");
    try {
      const p = await api(`/accounts/${accountId}/multileg/preview`, { method: "POST", body: body() });
      setPreview(p);
      if (!p.ok) setError(`This order would be rejected: ${p.reason}.`);
    } catch (e: any) { setError(e.message); setPreview(null); }
    setBusy(false);
  };

  const place = async () => {
    setBusy(true); setError("");
    try {
      const o = await api(`/accounts/${accountId}/multileg`, {
        method: "POST", body: { ...body(), client_id: `ui-ml-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` },
      });
      onPlaced(o.status === "filled"
        ? `Filled: ${name}, ${fmtQty(o.filled_qty)} × ${u} at net ${netText(o.avg_fill_price)}`
        : `Strategy order placed: ${name}, ${fmtQty(o.qty)} × ${u} (${o.status.replace("_", " ")})`);
    } catch (e: any) { setError(e.message); setBusy(false); }
  };

  const expOptions = (exps || []).map((e) => <option key={e.date} value={e.date}>{shortDate(e.date)} ({e.dte}d)</option>);
  const okPreview = preview && preview.ok;

  return (
    <div className="modal-bg sb-bg" onClick={onClose}>
      <div className="sb" role="dialog" aria-modal="true" aria-labelledby="sb-title" onClick={(e) => e.stopPropagation()}>
        <header className="sb-head">
          <h3 id="sb-title">{init.title || "Strategy builder"} · {u}</h3>
          <span className="muted">{spot != null ? `Price ${money(spot)}` : ""}</span>
          <button className="ghost" onClick={onClose}>Close</button>
        </header>
        <div className="sb-body">
          <div className="sb-col">
            <section className="sb-setup" aria-label="Template and expiry">
              <label>Strategy
                <select ref={first} value={template} onChange={(e) => pickTemplate(e.target.value as TemplateId)}>
                  {TEMPLATES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
                </select>
              </label>
              <div className="row">
                <label>{tpl.twoExpiries ? "Near expiry" : "Expiry"}
                  <select value={near} onChange={(e) => edit(() => { setNear(e.target.value); setNeedsTemplate(true); })}>{expOptions}</select>
                </label>
                {tpl.twoExpiries && (
                  <label>Later expiry
                    <select value={far} onChange={(e) => edit(() => { setFar(e.target.value); setNeedsTemplate(true); })}>{expOptions}</select>
                  </label>
                )}
              </div>
              <p className="ticket-hint">{tpl.hint} Changing the strategy or expiry rebuilds the legs.</p>
            </section>

            <section className="sb-legs" aria-label="Legs">
              <h4>Legs: <span className="sb-name">{name}</span></h4>
              {legs.map((l, i) => {
                const q = quotes[i];
                const strikes = strikesOf(l.expiry);
                const list = strikes.includes(l.strike) ? strikes : [l.strike, ...strikes].sort((a, b) => a - b);
                return (
                  <fieldset key={i} className="sb-leg">
                    <legend>Leg {i + 1}: {describeLeg(u, l)}</legend>
                    <div className="sb-leg-grid">
                      <label>Side
                        <select value={l.side} onChange={(e) => setLeg(i, { side: e.target.value as Side })}>
                          <option value="buy">Buy</option><option value="sell">Sell</option>
                        </select>
                      </label>
                      <label>Kind
                        <select value={l.kind} onChange={(e) => changeKind(i, e.target.value as Leg["kind"])}>
                          <option value="C">Call</option><option value="P">Put</option>
                          {(!hasStock || l.kind === "S") && <option value="S">Stock</option>}
                        </select>
                      </label>
                      <label>{l.kind === "S" ? "Shares per unit" : "Ratio"}
                        <input inputMode="numeric" value={l.ratio} onChange={(e) => setLeg(i, { ratio: Math.max(0, Math.floor(Number(e.target.value.replace(/\D/g, "")) || 0)) })} />
                      </label>
                      {l.kind !== "S" && (
                        <>
                          <label>Expiry
                            <select value={l.expiry} onChange={(e) => setLeg(i, { expiry: e.target.value })}>{expOptions}</select>
                          </label>
                          <label>Strike
                            <select value={l.strike} onChange={(e) => setLeg(i, { strike: Number(e.target.value) })}>
                              {list.map((k) => <option key={k} value={k}>{strikeText(k)}</option>)}
                            </select>
                          </label>
                        </>
                      )}
                    </div>
                    <div className="sb-leg-foot">
                      <span className="sb-quote" data-leg-quote={i}>
                        {q.px ? <>Bid {money(q.px.bid)} · Ask {money(q.px.ask)}</> : "No quote yet"}
                        <span className="tag">{sourceLabel(q.source)}</span>
                      </span>
                      <button className="ghost sm" aria-label={`Remove leg ${i + 1}`} onClick={() => edit(() => setLegs(legs.filter((_, j) => j !== i)))}>Remove</button>
                    </div>
                  </fieldset>
                );
              })}
              <button className="ghost sm" onClick={addLeg} disabled={legs.length >= 4}>{legs.length >= 4 ? "At most 4 legs" : "Add a leg"}</button>
            </section>

            <section className="sb-order" aria-label="Price and quantity">
              <div className="sb-net" aria-live="polite">
                <span>Net at market</span><b>{natural == null ? "–" : netText(natural)}</b>
                <span>Middle</span><b>{mid == null ? "–" : netText(mid)}</b>
                <span className="muted sb-net-note">Per share, from the leg quotes above; one unit costs or pays this × 100.</span>
              </div>
              <div className="row">
                <label>Quantity (units)
                  <input inputMode="numeric" value={qty} onChange={(e) => edit(() => setQty(e.target.value.replace(/\D/g, "")))} />
                </label>
                <label>Order type
                  <select value={type} onChange={(e) => edit(() => setType(e.target.value))}>
                    <option value="limit">Net limit</option><option value="market">Market</option>
                  </select>
                </label>
              </div>
              {type === "limit" && (
                <div className="row sb-limit">
                  <label>Net limit price
                    <input inputMode="decimal" value={limit} onChange={(e) => edit(() => { setLimit(e.target.value); setPriceTouched(true); })} />
                  </label>
                  <div className="sb-effect">
                    <span id="sb-effect-label">Price effect</span>
                    <div className="seg" role="group" aria-labelledby="sb-effect-label">
                      {(["debit", "credit"] as const).map((k) => (
                        <button key={k} className={effect === k ? "on" : ""} aria-pressed={effect === k}
                          onClick={() => edit(() => { setEffect(k); setPriceTouched(true); })}>{k === "debit" ? "Debit (you pay)" : "Credit (you receive)"}</button>
                      ))}
                    </div>
                  </div>
                </div>
              )}
              <div className="row">
                <label>Time in force
                  <select value={tif} onChange={(e) => edit(() => setTif(e.target.value))}>
                    {["day", "gtc", "gtd", "ioc", "fok"].map((k) => <option key={k} value={k}>{TIF_LABELS[k]}</option>)}
                  </select>
                </label>
                {tif === "gtd" && (
                  <label>Good till date
                    <input type="date" value={gtd} min={isoDay(0)} max={isoDay(366)} onChange={(e) => edit(() => setGtd(e.target.value))} />
                  </label>
                )}
              </div>
            </section>
          </div>

          <div className="sb-col">
            <section className="sb-payoff" aria-label="Payoff at expiry">
              <h4>Profit and loss at expiry{preview ? "" : " (estimate from the quotes)"}</h4>
              {shape && !shape.atExpiry ? (
                <p className="muted">The legs expire on different dates, so the payoff at expiry depends on what the later options are worth then and is not drawn.</p>
              ) : shape && spot != null ? (
                <PayoffChart legs={legs} net={chartNet!} qty={units} spot={spot} breakEvens={shape.breakEvens} />
              ) : <p className="muted">Waiting for quotes for every leg…</p>}
            </section>

            <div ref={result} className="sb-result">
            {error && <div className="notice bad" role="alert">{error}</div>}
            {legs.length < 2 && <p className="ticket-hint">A strategy order needs at least two legs.</p>}
            {okPreview && <PreviewFacts p={preview} />}
            </div>
          </div>
        </div>
        <footer className="sb-foot">
          {okPreview ? (
            <>
              <button className="ghost" onClick={() => setPreview(null)} disabled={busy}>Edit</button>
              <button className="primary" onClick={place} disabled={busy}>Place paper order</button>
            </>
          ) : (
            <button className="primary" onClick={review} disabled={busy || legs.length < 2 || !qty || legs.some((l) => !l.ratio)}>Review order</button>
          )}
        </footer>
      </div>
    </div>
  );
}

function PreviewFacts({ p }: { p: any }) {
  const row = (k: string, v: string, cls = "") => <><dt>{k}</dt><dd className={cls}>{v}</dd></>;
  const cost = p.estimated_cost;
  const profit = p.max_profit_unlimited ? "Unlimited" : p.max_profit == null ? "Not known (legs expire on different dates)" : usd(p.max_profit);
  const loss = p.max_loss_unlimited ? "Unlimited" : p.max_loss == null ? "Not known (legs expire on different dates)" : usd(p.max_loss);
  return (
    <section className="sb-preview" aria-label="Order preview">
      <h4>Preview</h4>
      <dl>
        {row("Net price", netText(p.price_effect === "credit" ? -p.net_price : p.net_price))}
        {row(cost >= 0 ? "Total cost" : "Total credit", usd(Math.abs(cost)))}
        {row("Fees", usd(p.estimated_fees))}
        {row(p.estimated_total >= 0 ? "You pay, with fees" : "You receive, after fees", usd(Math.abs(p.estimated_total)))}
        {row("Maximum profit", profit, "up")}
        {row("Maximum loss", loss, "down")}
        {row("Break-even", p.break_evens.length ? p.break_evens.map((b: number) => money(b)).join(" and ") : "–")}
        {row("Margin or cash held", `${usd(p.requirement_before)} → ${usd(p.requirement_after)} (${signed(p.margin_effect)})`)}
        {row("Buying power after", p.buying_power_after == null ? "Unchanged (closing order)" : usd(p.buying_power_after))}
        {!p.marketable && row("At this limit", `Waits: the legs now trade at ${netText(p.market_price_effect === "credit" ? -p.market_net_price : p.market_net_price)}`)}
      </dl>
      <table className="tight sb-preview-legs">
        <thead><tr><th>Leg</th><th className="num">Qty</th><th className="num">Price</th><th>Source</th></tr></thead>
        <tbody>
          {p.legs.map((l: any) => (
            <tr key={l.symbol}>
              <td><span className={l.side === "buy" ? "up" : "down"}>{l.side === "buy" ? "Buy" : "Sell"}</span> {l.description}</td>
              <td className="num">{fmtQty(l.qty)}</td>
              <td className="num">{money(l.estimated_price)}</td>
              <td><span className="tag">{sourceLabel(l.source)}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
      {p.warnings.map((w: string) => <p key={w} className="muted">{w}</p>)}
    </section>
  );
}

// Legs that close a group of option (and stock) positions: the opposite side of each, sized in units.
export function closingLegs(positions: any[]): { legs: { symbol: string; side: Side; ratio: number }[]; qty: number } {
  const gcd = (a: number, b: number): number => (b ? gcd(b, a % b) : a);
  const sizes = positions.map((p) => Math.round(Math.abs(p.qty)));
  const units = sizes.reduce((g, s) => gcd(g, s), 0) || 1;
  return {
    qty: units,
    legs: positions.map((p, i) => ({ symbol: p.symbol, side: p.qty > 0 ? "sell" : "buy", ratio: sizes[i] / units })),
  };
}

