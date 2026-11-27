import { createContext, useContext, useEffect, useRef, useState } from "react";

// The live stream's health, for any component that should stop acting on stale data (the order ticket,
// the connection-lost bar). App owns the WebSocket and provides the value.
export type Connection = {
  connected: boolean; // the WebSocket is open
  stale: boolean;     // it has been down for more than STALE_MS, so prices and balances on screen are frozen
  since: number | null; // when it went down (ms since epoch), or null while connected
};

export const STALE_MS = 3000;
export const ConnectionContext = createContext<Connection>({ connected: true, stale: false, since: null });
export const useConnection = () => useContext(ConnectionContext);

/** Turns the raw "connected" flag into the Connection value, with the grace period before "stale". */
export function useConnectionState(connected: boolean, onReconnect?: () => void): Connection {
  const [since, setSince] = useState<number | null>(() => Date.now()); // not connected yet on first render
  const [stale, setStale] = useState(false);
  const wasStale = useRef(false);
  const cb = useRef(onReconnect);
  cb.current = onReconnect;
  useEffect(() => {
    if (connected) {
      setSince(null); setStale(false);
      if (wasStale.current) cb.current?.();
      wasStale.current = false;
      return;
    }
    const start = since ?? Date.now();
    if (since == null) setSince(start);
    const t = setTimeout(() => { setStale(true); wasStale.current = true; }, Math.max(0, start + STALE_MS - Date.now()));
    return () => clearTimeout(t);
  }, [connected]);
  return { connected, stale, since };
}
