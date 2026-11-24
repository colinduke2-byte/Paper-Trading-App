import { createContext, FormEvent, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { SIGNED_OUT } from "./util";
import "./login.css";

// Sign-in for servers started with a passcode (LAN mode). Without a passcode the gate renders the app at once.

type State = { auth_required: boolean; authenticated: boolean; bypass: boolean; lan: boolean };
type Ctx = { state: State | null; signOut: () => void };

const AccessContext = createContext<Ctx>({ state: null, signOut: () => {} });

async function readState(): Promise<State | null> {
  try {
    const r = await fetch("/api/auth/state");
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

export function AccessGate({ children }: { children: ReactNode }) {
  // "checking" until the first answer; when the server cannot be reached the app shows its own error.
  const [phase, setPhase] = useState<"checking" | "in" | "out">("checking");
  const [state, setState] = useState<State | null>(null);
  const [notice, setNotice] = useState("");
  const [generation, setGeneration] = useState(0);
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  const refresh = useCallback(async () => {
    const s = await readState();
    setState(s);
    setPhase(s && s.auth_required && !s.authenticated ? "out" : "in");
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    const onOut = () => {
      if (phaseRef.current === "in") setNotice("Your sign-in has ended. Enter the passcode to continue.");
      setPhase("out");
    };
    window.addEventListener(SIGNED_OUT, onOut);
    return () => window.removeEventListener(SIGNED_OUT, onOut);
  }, []);

  const signOut = useCallback(async () => {
    phaseRef.current = "out";
    setPhase("out"); // unmount the app first, so nothing else is requested with the ending session
    setNotice("");
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch {}
    setNotice("You are signed out.");
  }, []);

  if (phase === "checking") return null;
  if (phase === "out") {
    return <Login notice={notice} onDone={() => { setNotice(""); setGeneration((g) => g + 1); refresh(); }} />;
  }
  return <AccessContext.Provider key={generation} value={{ state, signOut }}>{children}</AccessContext.Provider>;
}

// The sign-out action, or null when it does not apply (no passcode, or this computer skips it). A menu or a
// top-bar actions list can use this directly: `const so = useSignOutAction(); if (so) actions.push(so);`.
export function useSignOutAction(): { key: string; label: string; onClick: () => void } | null {
  const { state, signOut } = useContext(AccessContext);
  if (!state?.auth_required || state.bypass) return null;
  return { key: "sign-out", label: "Sign out", onClick: signOut };
}

export function SignOut() {
  const action = useSignOutAction();
  return action ? <button className="ghost sm" onClick={action.onClick}>{action.label}</button> : null;
}

function Login({ notice, onDone }: { notice: string; onDone: () => void }) {
  const [passcode, setPasscode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!passcode || busy) return;
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode }),
      });
      const j = await r.json().catch(() => null);
      if (r.ok && j?.ok) { onDone(); return; }
      setPasscode("");
      setError(j?.detail || `The server refused the sign-in (${r.status} ${r.statusText}).`);
    } catch {
      setError("Cannot reach the PaperDesk server. Check that it is running and that this device is on the same network.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit} aria-labelledby="login-title">
        <h1 id="login-title">PaperDesk</h1>
        <p className="login-sub">Paper trading · no real money</p>
        <p>Enter the passcode to open PaperDesk on this device.</p>
        {notice && !error && <div className="notice" role="status">{notice}</div>}
        <label>Passcode
          <input type="password" value={passcode} autoComplete="current-password" autoFocus autoCapitalize="none"
            autoCorrect="off" spellCheck={false} onChange={(e) => setPasscode(e.target.value)} />
        </label>
        {error && <div className="notice bad" role="alert">{error}</div>}
        <button className="primary" type="submit" disabled={busy || !passcode}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="muted login-foot">The connection is not encrypted. Use this only on your own home network.</p>
      </form>
    </div>
  );
}
