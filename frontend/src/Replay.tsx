import { useEffect, useRef, useState } from "react";
import { api, numText, usd } from "./util";
import "./replay.css";

// Replay times are shown in New York time, the exchange's own clock.
const NY = "America/New_York";
const fmtDate = new Intl.DateTimeFormat("en-US", { timeZone: NY, weekday: "short", day: "numeric", month: "short", year: "numeric" });
const fmtTime = new Intl.DateTimeFormat("en-GB", { timeZone: NY, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const fmtHM = new Intl.DateTimeFormat("en-GB", { timeZone: NY, hour: "2-digit", minute: "2-digit", hour12: false });
const localHM = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
const SPEEDS = [1, 2, 5, 10, 30, 60];

const sessionDate = (iso: string) => fmtDate.format(new Date(iso + "T12:00:00Z"));
const nyMinutes = (ts: number) => { const [h, m] = fmtHM.format(new Date(ts * 1000)).split(":").map(Number); return h * 60 + m; };
const sameZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone === NY;

/** Text for the paper banner while a replay runs, or null in live mode. */
export function replayBanner(status: any): string | null {
  const r = status?.replay;
  if (!r) return null;
  return `Replay of ${sessionDate(r.date)} · ${r.simulated ? "simulated market data" : "recorded market data"} · paper trading, no real money`;
}

/** Changes whenever the app switches between live and a replay run; use it as a React key. */
export function modeKey(status: any): string {
  return status ? (status.replay?.run_id || "live") : "";
}

/** Changes after each replay step or jump, so views that load history once (the chart) can reload. */
export function jumpKey(status: any): string {
  return status?.replay ? `${status.replay.run_id}:${status.replay.jumps}` : "live";
}

/** Calls onSwitch after the app moves between live and replay (not on the first status). */
export function useModeSwitch(status: any, onSwitch: () => void) {
  const key = modeKey(status);
  // Start from "live": if a replay starts (or is already running) before the first status arrives,
  // the switch is still seen and the accounts are reloaded.
  const prev = useRef("live");
  const cb = useRef(onSwitch);
  cb.current = onSwitch;
  useEffect(() => {
    if (!key) return;
    if (prev.current && prev.current !== key) cb.current();
    prev.current = key;
  }, [key]);
}

export function ReplayButton({ status, onOpen }: { status: any; onOpen: () => void }) {
  if (status?.replay) return null;
  return <button className="ghost sm replay-open" onClick={onOpen} title="Practise on a recorded trading session">Replay</button>;
}

export function ReplayPicker({ onClose, onStarted }: { onClose: () => void; onStarted: (status: any, msg: string) => void }) {
  const [sessions, setSessions] = useState<any[] | null>(null);
  const [pick, setPick] = useState("");
  const [cash, setCash] = useState("100000");
  const [type, setType] = useState("margin");
  const [speed, setSpeed] = useState(1);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api("/replay/sessions").then((rows) => {
      setSessions(rows);
      const first = rows.find((r: any) => !r.error);
      if (first) setPick(first.id);
    }).catch((e) => setError(`Could not load the session list: ${e.message}`));
  }, []);

  const start = async () => {
    setBusy(true); setError("");
    try {
      await api("/replay/start", { method: "POST", body: { session: pick, starting_cash: numText(cash), account_type: type, speed } });
      const st = await api("/status");
      const s = sessions?.find((x) => x.id === pick);
      onStarted(st, `Replay started: ${s ? sessionDate(s.date) : pick}. Press play when you are ready.`);
    } catch (e: any) { setError(e.message); setBusy(false); }
  };

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal replay-modal" role="dialog" aria-modal="true" aria-labelledby="replay-title" onClick={(e) => e.stopPropagation()}>
        <h3 id="replay-title">Replay a trading session</h3>
        <p className="muted">You trade in a separate sandbox account while the session plays back on its own clock. Your paper accounts are saved and left exactly as they are; the sandbox is discarded when you exit.</p>
        {sessions === null && !error && <p className="muted">Loading sessions…</p>}
        {sessions?.length === 0 && <p className="muted">No sessions found.</p>}
        {sessions && sessions.length > 0 && (
          <div className="replay-list" role="radiogroup" aria-label="Sessions">
            {sessions.map((s) => (
              <label key={s.id} className={"replay-item" + (s.id === pick ? " on" : "") + (s.error ? " broken" : "")}>
                <input type="radio" name="replay-session" value={s.id} checked={s.id === pick} disabled={!!s.error} onChange={() => setPick(s.id)} />
                <span className="replay-item-body">
                  <b>{s.error ? s.id : sessionDate(s.date)}</b>
                  {!s.error && <span className="tag">{s.simulated ? "Simulated data" : "Recorded data"}</span>}
                  <span className="muted">
                    {s.error ? `Cannot be replayed: ${s.error}` : `${fmtHM.format(new Date(s.start * 1000))}–${fmtHM.format(new Date((s.end ?? s.start) * 1000))} ET · ${s.symbols.length} symbols`}
                  </span>
                  {s.description && <span className="muted small">{s.description}</span>}
                </span>
              </label>
            ))}
          </div>
        )}
        <div className="row replay-fields">
          <label>Starting cash<input value={cash} inputMode="decimal" onChange={(e) => setCash(e.target.value)} /></label>
          <label>Account type
            <select value={type} onChange={(e) => setType(e.target.value)}>
              <option value="margin">Margin</option>
              <option value="cash">Cash</option>
            </select>
          </label>
          <label>Speed
            <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
              {SPEEDS.map((x) => <option key={x} value={x}>{x}x</option>)}
            </select>
          </label>
        </div>
        {error && <div className="notice bad" role="alert">{error}</div>}
        <div className="row">
          <button className="ghost" onClick={onClose}>Cancel</button>
          <button className="primary" disabled={!pick || busy} onClick={start}>{busy ? "Starting…" : `Start with ${usd(Number(numText(cash)) || 0)}`}</button>
        </div>
      </div>
    </div>
  );
}

export function ReplayBar({ status, onStatus, notify }: { status: any; onStatus: (s: any) => void; notify: (m: string, bad?: boolean) => void }) {
  const r = status?.replay;
  const [drag, setDrag] = useState<number | null>(null);
  const [jump, setJump] = useState("");
  const [busy, setBusy] = useState(false);
  if (!r) return null;
  const span = Math.max(1, r.end - r.start);
  const pos = drag ?? r.time - r.start;

  const send = async (path: string, body?: any) => {
    setBusy(true);
    try {
      await api(`/replay/${path}`, { method: "POST", body: body ?? {} });
      onStatus(await api("/status"));
    } catch (e: any) { notify(e.message, true); }
    finally { setBusy(false); }
  };
  const seekTo = (offset: number) => {
    setDrag(null);
    if (offset <= r.time - r.start) { notify("A replay cannot go back in time. Exit and start the session again to see an earlier part.", true); return; }
    send("seek", { offset });
  };
  const doJump = () => {
    const m = jump.match(/^(\d{1,2}):(\d{2})$/);
    if (!m) { notify("Enter a time as HH:MM, New York time.", true); return; }
    // Offset from the session start; time zones shift by whole minutes, so the start's seconds carry over.
    const offset = (Number(m[1]) * 60 + Number(m[2]) - nyMinutes(r.start)) * 60 - (r.start % 60);
    if (offset > span) { notify(`The session ends at ${fmtHM.format(new Date(r.end * 1000))} New York time.`, true); return; }
    seekTo(offset);
  };
  const exit = () => send("exit").then(() => notify("Replay closed. You are back on your paper accounts."));

  return (
    <div className={"replay-bar" + (r.ended ? " ended" : "")} role="region" aria-label="Replay controls">
      <div className="replay-when">
        <span className="replay-badge">Replay</span>
        <b>{sessionDate(r.date)}</b>
        <span className="replay-clock" aria-live="off">{fmtTime.format(new Date(r.time * 1000))} ET</span>
        {!sameZone() && <span className="muted replay-local" title="Your local time, as on the chart">{localHM(r.time)} your time</span>}
      </div>
      <div className="replay-controls">
        <button className="ghost replay-play" disabled={busy || r.ended} onClick={() => send(r.paused ? "resume" : "pause")}
          aria-label={r.paused ? "Play" : "Pause"}>{r.paused ? "▶ Play" : "❚❚ Pause"}</button>
        <button className="ghost" disabled={busy || r.ended} onClick={() => send("step", { seconds: 60 })} title="Advance one minute">+1 min</button>
        <button className="ghost" disabled={busy || r.ended} onClick={() => send("step", { seconds: 900 })} title="Advance fifteen minutes">+15 min</button>
        <div className="seg replay-speed" role="group" aria-label="Speed">
          {SPEEDS.map((x) => <button key={x} className={x === r.speed ? "on" : ""} onClick={() => send("speed", { speed: x })} aria-pressed={x === r.speed}>{x}x</button>)}
        </div>
      </div>
      <div className="replay-progress">
        <input type="range" min={0} max={span} step={60} value={pos} aria-label="Session progress"
          aria-valuetext={`${fmtTime.format(new Date((r.start + pos) * 1000))} New York time`}
          onChange={(e) => setDrag(Number(e.target.value))}
          onPointerUp={() => drag !== null && seekTo(drag)} onKeyUp={() => drag !== null && seekTo(drag)} />
        <span className="muted">{Math.round(r.progress * 100)}%</span>
      </div>
      <form className="replay-jump" onSubmit={(e) => { e.preventDefault(); doJump(); }}>
        <label className="inline">Jump to
          <input value={jump} placeholder="HH:MM" inputMode="numeric" aria-label="Jump to New York time" onChange={(e) => setJump(e.target.value)} />
        </label>
        <button className="ghost" type="submit" disabled={busy || r.ended}>Go</button>
      </form>
      {r.ended && <span className="replay-ended">Session ended</span>}
      {r.lagging && !r.paused && <span className="muted" title="The computer cannot keep up with this speed">Running behind</span>}
      <button className="ghost replay-exit" disabled={busy} onClick={exit}>Exit replay</button>
    </div>
  );
}
