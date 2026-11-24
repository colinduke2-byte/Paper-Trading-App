import { useState } from "react";
import { api } from "./util";

/**
 * Shown when the server lists the selected account as "broken": it cannot be valued (for example an impossible
 * number from an old saved state). Every other account keeps working; this one can be reset or deleted.
 */
export function AccountFault({ account, onReset, onDeleted }: { account: any; onReset: () => void; onDeleted: (msg: string) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (!account?.broken) return null;
  const remove = async () => {
    if (!window.confirm(`Delete "${account.name}"? Its positions, orders and history are erased. This cannot be undone.`)) return;
    setBusy(true);
    try {
      await api(`/accounts/${account.id}`, { method: "DELETE" });
      onDeleted(`Account "${account.name}" deleted`);
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  return (
    <div className="notice bad wide" role="alert">
      {account.error || "This account cannot be valued."}
      <div className="row">
        <button className="ghost sm" onClick={onReset} disabled={busy}>Reset with new cash</button>
        <button className="ghost sm" onClick={remove} disabled={busy}>Delete account</button>
      </div>
      {error && <div>{error}</div>}
    </div>
  );
}
