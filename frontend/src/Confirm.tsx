import { useEffect, useRef, useState } from "react";

// A small in-app confirmation dialog for actions that are hard to undo. Call confirmAction() from anywhere;
// <ConfirmHost/> (rendered once by App) shows it. Works the same on a phone and a desktop.

export type ConfirmOptions = { title: string; body?: string; confirm: string; cancel?: string; danger?: boolean };
type Pending = ConfirmOptions & { resolve: (ok: boolean) => void };

let show: ((p: Pending) => void) | null = null;

/** Resolves true when the person confirms, false when they cancel (or nothing can show the dialog). */
export function confirmAction(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => (show ? show({ ...opts, resolve }) : resolve(window.confirm(`${opts.title}\n\n${opts.body || ""}`))));
}

export function ConfirmHost() {
  const [p, setP] = useState<Pending | null>(null);
  const cancelBtn = useRef<HTMLButtonElement>(null);
  useEffect(() => { show = setP; return () => { show = null; }; }, []);
  useEffect(() => {
    if (!p) return;
    cancelBtn.current?.focus(); // the safe choice has focus, so Enter does not destroy anything
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
    window.addEventListener("keydown", esc, true);
    return () => window.removeEventListener("keydown", esc, true);
  }, [p]);
  if (!p) return null;
  const done = (ok: boolean) => { p.resolve(ok); setP(null); };
  return (
    <div className="modal-bg confirm-bg" onClick={() => done(false)}>
      <div className="modal confirm" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-body"
        onClick={(e) => e.stopPropagation()}>
        <h3 id="confirm-title">{p.title}</h3>
        {p.body && <p id="confirm-body" className="muted">{p.body}</p>}
        <div className="row">
          <button ref={cancelBtn} className="ghost" onClick={() => done(false)}>{p.cancel || "Cancel"}</button>
          <button className={p.danger ? "danger" : "primary"} onClick={() => done(true)}>{p.confirm}</button>
        </div>
      </div>
    </div>
  );
}
