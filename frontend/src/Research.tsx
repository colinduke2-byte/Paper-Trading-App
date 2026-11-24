import { usePoll } from "./Market";
import { safeUrl } from "./safeUrl";
import { compact, dateTime, money } from "./util";
import "./Research.css";

type Envelope = { source: string | null; as_of: number | null; available: boolean; stale: boolean; reason: string | null; items?: any[]; data?: any };

/** Source line shown on every research block: provider, time of the data, and a stale or unavailable state. */
function SourceLine({ env, error }: { env: Envelope | null; error: string }) {
  if (error) return <div className="rs-state bad" role="status">Could not load this section: {error}</div>;
  if (!env) return <div className="rs-state muted">Loading…</div>;
  if (!env.available) return <div className="rs-state" role="status">Unavailable. {env.reason}</div>;
  return (
    <div className={env.stale ? "rs-source rs-stale" : "rs-source"}>
      Source: {env.source}{env.as_of ? ` · updated ${dateTime(env.as_of)}` : ""}
      {env.stale && <span className="rs-flag"> · stale: {env.reason}</span>}
    </div>
  );
}

function News({ symbol }: { symbol: string }) {
  const [env, error] = usePoll<Envelope>(`/research/news/${symbol}?limit=20`, 120000);
  const items = env?.available ? env.items || [] : [];
  return (
    <section className="rs-block" aria-labelledby="rs-news">
      <h3 id="rs-news">News</h3>
      <SourceLine env={env} error={error} />
      {env?.available && !items.length && <p className="muted rs-pad">No recent articles for {symbol}.</p>}
      <ul className="rs-news">
        {items.map((n) => (
          <li key={n.id ?? n.url}>
            {safeUrl(n.url)
              ? <a href={safeUrl(n.url)} target="_blank" rel="noopener noreferrer">{n.headline}</a>
              : <span>{n.headline}</span>}
            <div className="muted rs-meta">{n.source}{n.created_at ? ` · ${dateTime(n.created_at)}` : ""}</div>
            {n.summary && <p className="rs-summary">{stripTags(n.summary)}</p>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Calendar({ symbol }: { symbol: string }) {
  const [cal, error] = usePoll<any>(`/research/calendar/${symbol}`, 600000);
  const today = new Date().toISOString().slice(0, 10);
  return (
    <section className="rs-block" aria-labelledby="rs-cal">
      <h3 id="rs-cal">Earnings</h3>
      <SourceLine env={cal?.earnings ?? null} error={error} />
      {cal?.earnings?.available && (
        cal.earnings.items.length ? (
          <ul className="rs-list">
            {cal.earnings.items.slice().reverse().map((e: any) => (
              <li key={`${e.date}-${e.quarter}`} className={e.date >= today ? "rs-upcoming" : ""}>
                <b>{e.date}</b>{e.hour ? <span className="muted"> {e.hour}</span> : null}
                <span>Q{e.quarter} {e.year}</span>
                <span className="muted">EPS est. {money(e.eps_estimate)}{e.eps_actual != null ? ` · actual ${money(e.eps_actual)}` : ""}</span>
              </li>
            ))}
          </ul>
        ) : <p className="muted rs-pad">No earnings dates in the free window (one month back, four months ahead).</p>
      )}
      <h3>Dividends and splits</h3>
      <SourceLine env={cal?.dividends ?? null} error={error} />
      {cal?.dividends?.available && (
        cal.dividends.items.length || cal.splits.items.length ? (
          <ul className="rs-list">
            {[...cal.dividends.items, ...cal.splits.items].sort((a: any, b: any) => (b.ex_date || "").localeCompare(a.ex_date || "")).map((r: any) => (
              <li key={r.id} className={(r.ex_date || "") >= today ? "rs-upcoming" : ""}>
                <b>{r.ex_date || r.process_date}</b>
                <span>{r.type === "cash_dividend" ? `Dividend ${money(r.rate, 4)} per share` : `${r.type === "forward_split" ? "Split" : "Reverse split"} ${r.new_rate}-for-${r.old_rate}`}</span>
                <span className="muted">{r.type === "cash_dividend" ? `pays ${r.payable_date || "–"}${r.special ? " · special" : ""}` : "ex-date"}</span>
              </li>
            ))}
          </ul>
        ) : <p className="muted rs-pad">No dividends or splits in the last year or announced.</p>
      )}
    </section>
  );
}

function Fundamentals({ symbol }: { symbol: string }) {
  const [env, error] = usePoll<Envelope>(`/research/fundamentals/${symbol}`, 0);
  const f = env?.available ? env.data : null;
  const row = (k: string, v: string) => <div key={k}><span>{k}</span><b>{v}</b></div>;
  return (
    <section className="rs-block" aria-labelledby="rs-fund">
      <h3 id="rs-fund">Fundamentals</h3>
      <SourceLine env={env} error={error} />
      {f && (
        <div className="rs-facts">
          {row("Name", f.name || "–")}
          {row("Industry", f.industry || "–")}
          {row("Market cap", compact(f.market_cap))}
          {row("P/E (trailing)", f.pe == null ? "–" : f.pe.toFixed(1))}
          {row("EPS (trailing)", money(f.eps))}
          {row("Dividend yield", f.dividend_yield_pct == null ? "–" : f.dividend_yield_pct.toFixed(2) + "%")}
          {row("Beta", f.beta == null ? "–" : f.beta.toFixed(2))}
          {row("52-week range", f.low_52w == null ? "–" : `${money(f.low_52w)} – ${money(f.high_52w)}`)}
        </div>
      )}
    </section>
  );
}

function stripTags(s: string) {
  return s.replace(/<[^>]*>/g, "").replace(/&amp;/g, "&").replace(/&#39;|&apos;/g, "'").replace(/&quot;/g, '"');
}

export function Research({ symbol }: { symbol: string }) {
  const [sources] = usePoll<any>("/research/sources", 0);
  if (sources?.reason) {  // the whole panel is unavailable for one reason (e.g. the synthetic feed): say it once
    return <div className="research"><div className="notice rs-notice" role="status">Research data is unavailable. {sources.reason}</div></div>;
  }
  return (
    <div className="research">
      <News symbol={symbol} />
      <div className="rs-side">
        <Calendar symbol={symbol} />
        <Fundamentals symbol={symbol} />
      </div>
    </div>
  );
}

/** Banner suffix naming the market data source, used by the paper-trading banner in App.tsx. */
export function feedBanner(s: any): { text: string; title: string } {
  if (!s) return { text: "", title: "" };
  const title = [s.data_note, s.fallback_reason, s.stream?.message].filter(Boolean).join(" ");
  if (s.simulated_data) return { text: " · simulated market data", title };
  const src = s.data_source || s.feed;
  if (s.data_warning) return { text: ` · ${src} feed sent invalid data`, title: [s.data_warning, title].filter(Boolean).join(" ") };
  if (s.stale) return { text: ` · ${src} prices may be stale (feed interrupted)`, title };
  if (s.realtime === false || s.delay_seconds) return { text: ` · ${src} data delayed ${Math.round((s.delay_seconds || 900) / 60)} min`, title };
  return { text: ` · real-time ${s.data_exchange || ""} prices via ${src}`.replace("  ", " "), title };
}
