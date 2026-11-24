import { useState } from "react";
import { usePoll } from "./Market";
import { qty, signed, tone, usd } from "./util";
import "./lots.css";

// Reports > Tax year: short-term and long-term totals for one calendar year, with the informational
// wash-sale adjustments. WashDetail is the wash-sale cell of the realised gains table.

export function WashDetail({ row }: { row: any }) {
  const [open, setOpen] = useState(false);
  if (!row.wash_sale && !row.basis_adjustment) return <span className="muted">–</span>;
  return (
    <span className="rep-wash">
      {row.wash_sale && (
        <button className="flag" aria-expanded={open} onClick={() => setOpen(!open)}
          title="Informational only, not tax advice. A loss with replacement shares bought within 30 days.">
          {usd(row.wash_sale_disallowed)} disallowed
        </button>
      )}
      {row.basis_adjustment > 0 && <span className="muted"> Basis +{usd(row.basis_adjustment)} carried in</span>}
      {open && (
        <ul className="rep-notes">
          {row.wash_sale_with.map((x: any) => (
            <li key={x.key}>
              Replacement {x.lot_id || x.fill_id} opened {x.open_date}: {qty(x.qty)} at basis {usd(x.basis_before)}
              {" "}becomes {usd(x.basis_after)}, holding period from {x.adjusted_open_date} ({x.status === "open" ? "still open" : "closed"})
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}

function Bucket({ label, b, adjusted }: { label: string; b: any; adjusted: number }) {
  return (
    <tr>
      <td>{label}</td><td className="num">{b.lots}</td><td className="num">{usd(b.proceeds)}</td><td className="num">{usd(b.cost)}</td>
      <td className={"num " + tone(b.gain)}>{signed(b.gain)}</td><td className="num">{usd(b.wash_sale_disallowed)}</td>
      <td className={"num " + tone(adjusted)}>{signed(adjusted)}</td>
    </tr>
  );
}

export function TaxYear({ accountId, refreshKey }: { accountId: string; refreshKey: number }) {
  const [year, setYear] = useState<number | null>(null);
  const [data, error] = usePoll<any>(`/accounts/${accountId}/tax-summary${year ? `?year=${year}` : ""}`, 0, [refreshKey, year]);
  if (error) return <div className="notice bad" role="alert">Could not load the tax year summary: {error}</div>;
  if (!data) return <p className="muted pad">Loading the tax year summary…</p>;
  const years: number[] = data.years.length ? data.years : [data.year];
  return (
    <div className="rep-body">
      <div className="toolbar">
        <label className="inline">Year
          <select value={data.year} aria-label="Tax year" onChange={(e) => setYear(Number(e.target.value))}>
            {years.map((y) => <option key={y} value={y}>{y}</option>)}
          </select>
        </label>
        <span className="muted rep-label">Informational only, not tax advice.</span>
      </div>
      <div className="rep-table">
        <table className="rep-summary">
          <thead><tr><th>Holding period</th><th className="num">Lots</th><th className="num">Proceeds</th><th className="num">Cost</th>
            <th className="num">Gain</th><th className="num">Wash-sale disallowed</th><th className="num">Adjusted gain</th></tr></thead>
          <tbody>
            <Bucket label="Short-term" b={data.short_term} adjusted={data.adjusted.short_term} />
            <Bucket label="Long-term" b={data.long_term} adjusted={data.adjusted.long_term} />
            <tr className="total"><td>Total</td><td className="num">{data.total.lots}</td><td className="num">{usd(data.total.proceeds)}</td>
              <td className="num">{usd(data.total.cost)}</td><td className={"num " + tone(data.total.gain)}>{signed(data.total.gain)}</td>
              <td className="num">{usd(data.total.wash_sale_disallowed)}</td>
              <td className={"num " + tone(data.adjusted.total)}>{signed(data.adjusted.total)}</td></tr>
          </tbody>
        </table>
      </div>
      <p className="muted rep-note">Adjusted gain adds back losses disallowed by wash sales and includes basis carried in from earlier
        ones; its short-term and long-term split uses the adjusted holding period, so it can differ from the rows above.</p>
      <h4 className="rep-h">Wash sales in {data.year}</h4>
      {data.wash_sales.length === 0 ? <p className="muted pad">No losses this year had replacement shares within 30 days.</p> : (
        <div className="rep-table">
          <table>
            <thead><tr><th>Closed lot</th><th>Instrument</th><th>Closed</th><th className="num">Qty</th><th className="num">Loss</th>
              <th className="num">Disallowed</th><th>Replacement lots</th></tr></thead>
            <tbody>
              {data.wash_sales.map((w: any) => (
                <tr key={w.id}>
                  <td>{w.lot_id || w.id}</td><td>{w.description}</td><td>{w.close_date}</td><td className="num">{qty(w.qty)}</td>
                  <td className="num down">{usd(-w.loss)}</td><td className="num">{usd(w.disallowed)}</td>
                  <td>{w.replacements.map((x: any) => `${x.lot_id || x.fill_id} (${qty(x.qty)}, basis ${usd(x.basis_after)}, held from ${x.adjusted_open_date})`).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ul className="rep-notes muted">{data.notes.map((n: string) => <li key={n}>{n}</li>)}</ul>
    </div>
  );
}
