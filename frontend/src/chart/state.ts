// Chart settings and drawings: cached in localStorage for an instant start, saved on the server
// (/api/chartstate) so they follow the user to another browser. Each record carries an `updated` stamp and the
// newer of the local and server copies wins.
import { useCallback, useEffect, useRef, useState } from "react";
import { IndicatorConfig, migrateLegacy, newId, parseIndicators } from "../indicators";
import { Drawing, parseDrawings } from "./drawings";
import { api } from "../util";

export type ChartType = "candles" | "bars" | "line" | "area";
export const CHART_TYPES: Record<ChartType, string> = { candles: "Candles", bars: "OHLC bars", line: "Line", area: "Area" };
export type ChartSettings = { type: ChartType; log: boolean; indicators: IndicatorConfig[]; compare: string };
type Rec<T> = { updated: number; data: T };

const SETTINGS_KEY = "pd.chart.settings";
// Live drawings keep the original key; each replay session has its own.
const drawingsKey = (scope: string, s: string) => (scope === "live" ? `pd.chart.drawings.${s}` : `pd.chart.drawings.${scope}.${s}`);
const scopeQuery = (scope: string) => (scope === "live" ? "" : `?scope=${encodeURIComponent(scope)}`);

function readLocal(key: string): Rec<unknown> | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const v = JSON.parse(raw);
    return v && typeof v === "object" && typeof v.updated === "number" ? v : null;
  } catch { return null; }
}
function writeLocal(key: string, rec: Rec<unknown>) {
  try { localStorage.setItem(key, JSON.stringify(rec)); } catch {}
}

export function parseSettings(raw: any): ChartSettings {
  const type = raw && raw.type in CHART_TYPES ? raw.type : "candles";
  const compare = typeof raw?.compare === "string" && /^[A-Z0-9.\-]{0,24}$/.test(raw.compare) ? raw.compare : "";
  return { type, log: raw?.log === true, indicators: parseIndicators(raw?.indicators), compare };
}

function initialSettings(): Rec<ChartSettings> {
  const local = readLocal(SETTINGS_KEY);
  if (local) return { updated: local.updated, data: parseSettings(local.data) };
  let legacy: string | null = null;
  try { legacy = localStorage.getItem("pd.inds"); } catch {}
  const indicators = legacy != null ? migrateLegacy(legacy) : [{ id: newId(), type: "sma" as const, params: { period: 20 } }];
  return { updated: 0, data: { type: "candles", log: false, indicators, compare: "" } };
}

function debounce(fn: (leaving: boolean) => void, ms: number) {
  let t: any = null;
  const flush = (leaving = false) => { if (t) { clearTimeout(t); t = null; fn(leaving); } };
  // Save straight away when the page is being left (reload, close, navigation) so nothing is lost.
  const onLeave = () => flush(true);
  if (typeof window !== "undefined") window.addEventListener("pagehide", onLeave);
  return {
    run: () => { clearTimeout(t); t = setTimeout(() => { t = null; fn(false); }, ms); },
    flush: () => flush(false),
    /** Save anything pending and stop listening (the chart is unmounting, for example after a replay jump). */
    dispose: () => { flush(false); if (typeof window !== "undefined") window.removeEventListener("pagehide", onLeave); },
  };
}

/** A value made once per mounted component (useRef(make()) would call make on every render). */
function useOnce<T>(make: () => T): { current: T } {
  const [v] = useState(() => ({ current: make() }));
  return v;
}

/** PUT a record. `keepalive` lets the request finish while the page unloads. */
function put(path: string, body: unknown, keepalive: boolean): Promise<any> {
  if (!keepalive) return api(path, { method: "PUT", body });
  return fetch("/api" + path, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive: true })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText))));
}

/** Settings shared by every symbol: chart type, scale, indicators and compare symbol. */
export function useChartSettings(onError: (msg: string) => void): [ChartSettings, (s: ChartSettings) => void] {
  const [rec, setRec] = useState(initialSettings);
  const recRef = useRef(rec);
  recRef.current = rec;
  const errRef = useRef(onError);
  errRef.current = onError;
  const saver = useOnce(() => debounce((leaving) => {
    put("/chartstate/settings", recRef.current, leaving).catch((e) => { if (!leaving) errRef.current(`Could not save chart settings on the server: ${e.message}`); });
  }, 400));

  useEffect(() => {
    let alive = true;
    api("/chartstate").then((st) => {
      const srv = st?.settings;
      const local = recRef.current;
      // A local copy newer than the server's (saved while offline, or just before a reload) is sent up.
      if (local.updated > 0 && (!srv || typeof srv.updated !== "number" || srv.updated < local.updated)) { saver.current.run(); return; }
      if (!alive || !srv || typeof srv.updated !== "number" || srv.updated <= recRef.current.updated) return;
      const next = { updated: srv.updated, data: parseSettings(srv.data) };
      writeLocal(SETTINGS_KEY, next);
      setRec(next);
    }).catch(() => {});
    return () => { alive = false; saver.current.dispose(); };
  }, []);

  const set = useCallback((data: ChartSettings) => {
    const next = { updated: Math.max(Date.now(), recRef.current.updated + 1), data };
    recRef.current = next;
    writeLocal(SETTINGS_KEY, next);
    setRec(next);
    saver.current.run();
  }, []);
  return [rec.data, set];
}

/**
 * Drawings for one symbol in one scope: "live", or "replay:<session id>" (a recorded session is a different price
 * series, so its drawings are kept apart from the live ones). The chart is remounted when the scope changes.
 */
export function useDrawings(symbol: string, scope: string, onError: (msg: string) => void): [Drawing[], (d: Drawing[]) => void] {
  const key = `${scope}|${symbol}`;
  const load = (sc: string, s: string): Rec<Drawing[]> => {
    const local = readLocal(drawingsKey(sc, s));
    return local ? { updated: local.updated, data: parseDrawings(local.data) } : { updated: 0, data: [] };
  };
  const [state, setState] = useState(() => ({ key, scope, symbol, rec: load(scope, symbol) }));
  const ref = useRef(state);
  ref.current = state;
  const errRef = useRef(onError);
  errRef.current = onError;
  const pending = useRef<Record<string, { scope: string; symbol: string; rec: Rec<Drawing[]> }>>({});
  const saver = useOnce(() => debounce((leaving) => {
    const all = pending.current;
    pending.current = {};
    for (const p of Object.values(all))
      put(`/chartstate/drawings/${encodeURIComponent(p.symbol)}${scopeQuery(p.scope)}`, p.rec, leaving)
        .catch((e) => { if (!leaving) errRef.current(`Could not save drawings on the server: ${e.message}`); });
  }, 300));

  useEffect(() => {
    if (ref.current.key !== key) setState({ key, scope, symbol, rec: load(scope, symbol) });
    let alive = true;
    api(`/chartstate/drawings/${encodeURIComponent(symbol)}${scopeQuery(scope)}`).then((srv) => {
      if (!alive) return;
      const cur = ref.current;
      if (cur.key !== key) return;
      if (cur.rec.updated > 0 && (!srv || typeof srv.updated !== "number" || srv.updated < cur.rec.updated)) {
        pending.current[key] = { scope, symbol, rec: cur.rec }; // the local copy is newer: send it up
        saver.current.run();
        return;
      }
      if (!srv || typeof srv.updated !== "number" || srv.updated <= cur.rec.updated) return;
      const next = { updated: srv.updated, data: parseDrawings(srv.data) };
      writeLocal(drawingsKey(scope, symbol), next);
      setState({ key, scope, symbol, rec: next });
    }).catch(() => {});
    return () => { alive = false; };
  }, [key]);
  useEffect(() => () => saver.current.dispose(), []);

  const set = useCallback((data: Drawing[]) => {
    const cur = ref.current;
    const next = { updated: Math.max(Date.now(), cur.rec.updated + 1), data };
    const st = { ...cur, rec: next };
    ref.current = st;
    writeLocal(drawingsKey(cur.scope, cur.symbol), next);
    setState(st);
    pending.current[cur.key] = { scope: cur.scope, symbol: cur.symbol, rec: next };
    saver.current.run();
  }, []);
  // While the symbol is switching, show nothing rather than the previous symbol's drawings.
  return [state.key === key ? state.rec.data : [], set];
}
