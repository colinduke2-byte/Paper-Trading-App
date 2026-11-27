import { useEffect, useId, useRef, useState } from "react";
import { api } from "./util";

export type SymbolHit = { symbol: string; name: string; type: string; exchange?: string | null; sector?: string | null };

export const typeBadge = (t: string) => (t === "etf" ? "ETF" : t === "crypto" ? "Crypto" : "Stock");

// Ticker or company-name search, ranked on the server (bounded results, fast with 11,000 symbols).
// Arrow keys move through the results, Enter picks, Escape closes. With an empty box the recently
// picked symbols are offered. With `assetFilter`, an All / Stocks / Crypto choice narrows the search, and
// choosing a class with an empty box lists that class (crypto pairs, PR 14).
const CLASSES: [string, string][] = [["", "All"], ["stock", "Stocks"], ["crypto", "Crypto"]];
const TYPES: Record<string, string> = { stock: "stock,etf", crypto: "crypto" };

export function SymbolSearch({ onPick, label = "Search symbols", placeholder = "Search symbol or company", exclude = [], assetFilter = false }: {
  onPick: (symbol: string) => void; label?: string; placeholder?: string; exclude?: string[]; assetFilter?: boolean;
}) {
  const [q, setQ] = useState("");
  const [cls, setCls] = useState("");
  const [open, setOpen] = useState(false);
  const [hits, setHits] = useState<SymbolHit[]>([]);
  const [recent, setRecent] = useState<SymbolHit[]>([]);
  const [active, setActive] = useState(0);
  const [error, setError] = useState("");
  const id = useId();
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const term = q.trim();
    if (!term && !cls) { setHits([]); setError(""); return; }
    let alive = true;
    const t = setTimeout(() => {
      const url = term ? `/search?q=${encodeURIComponent(term.slice(0, 60))}&limit=8${cls ? `&types=${TYPES[cls]}` : ""}`
        : `/symbols?asset_class=${cls}&limit=50`; // the whole class, e.g. every crypto pair
      api(url)
        .then((r) => { if (alive) { setHits(r); setActive(0); setError(""); } })
        .catch((e) => alive && setError(e.message));
    }, 120);
    return () => { alive = false; clearTimeout(t); };
  }, [q, cls]);

  useEffect(() => {
    if (!open || q.trim()) return;
    let alive = true;
    api("/search/recent").then((r) => alive && setRecent(r)).catch(() => {});
    return () => { alive = false; };
  }, [open, q]);

  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [open]);

  const showingRecent = !q.trim() && !cls;
  const list = (showingRecent ? recent : hits).filter((h) => !exclude.includes(h.symbol));
  const pick = (s: string) => {
    onPick(s);
    api("/search/recent", { method: "POST", body: { symbol: s } }).catch(() => {});
    setQ(""); setCls(""); setOpen(false); setActive(0);
  };
  const expanded = open && (list.length > 0 || (!showingRecent && !error));
  const optId = (i: number) => `${id}-o${i}`;

  return (
    <div className="search" ref={box}>
      {assetFilter && (
        <div className="asset-filter seg" role="group" aria-label="Asset class">
          {CLASSES.map(([k, text]) => (
            <button key={k} type="button" className={cls === k ? "on" : ""} aria-pressed={cls === k}
              onClick={() => { setCls(k); setOpen(!!k || !!q.trim()); }}>{text}</button>
          ))}
        </div>
      )}
      <input value={q} placeholder={cls === "crypto" ? "Search coin" : placeholder} aria-label={label} role="combobox" aria-autocomplete="list"
        aria-expanded={expanded} aria-controls={`${id}-list`} aria-activedescendant={expanded && list[active] ? optId(active) : undefined}
        autoComplete="off" spellCheck={false}
        onFocus={() => setOpen(true)}
        onChange={(e) => { setQ(e.target.value); setOpen(true); }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive((a) => Math.min(a + 1, Math.max(list.length - 1, 0))); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
          else if (e.key === "Enter") { const h = list[active] || list[0]; if (h) { e.preventDefault(); pick(h.symbol); } }
          else if (e.key === "Escape") { if (q) setQ(""); else { setCls(""); setOpen(false); } }
        }} />
      {expanded && (
        <ul className="results" id={`${id}-list`} role="listbox" aria-label={showingRecent ? "Recent symbols" : "Matching symbols"}>
          {showingRecent && <li className="muted res-head" role="presentation">Recent</li>}
          {!showingRecent && list.length === 0 && <li className="muted" role="presentation">{error ? `Search failed: ${error}` : "No matching symbol"}</li>}
          {list.map((h, i) => (
            <li key={h.symbol} id={optId(i)} role="option" aria-selected={i === active}>
              <button tabIndex={-1} className={i === active ? "on" : ""} onMouseEnter={() => setActive(i)}
                onMouseDown={(e) => e.preventDefault()} onClick={() => pick(h.symbol)}>
                <b>{h.symbol}</b><span className="muted res-name">{h.name}</span>
                <span className={`badge badge-${h.type}`}>{typeBadge(h.type)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
