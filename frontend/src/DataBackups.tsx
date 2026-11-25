import { ChangeEvent, ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { api, dateTime, signedOut, usd } from "./util";
import "./data.css";

// "Data and backups": save status, backups (list, download, restore with a dry run), account export and
// import, and the recovery screen shown when the saved state could not be read. Server side:
// backend/app/api/routes_data.py.

type Backup = { name: string; kind: string; created: string; created_ts: number; size: number };
type Recovery = { kind: "damaged" | "newer"; reason: string; copy?: string | null };

const KIND: Record<string, string> = {
  auto: "Automatic", manual: "Manual", "pre-reset": "Before a reset", "pre-delete": "Before deleting an account",
  "pre-restore": "Before a restore", "pre-import": "Before an import", "pre-migration": "Before a format update",
  "pre-recovery": "Before recovery",
};

const size = (n: number) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

async function upload(path: string, file: Blob, type: string): Promise<any> {
  const r = await fetch("/api" + path, { method: "POST", headers: { "Content-Type": type }, body: file });
  const j = await r.json().catch(() => null);
  if (r.status === 401 && j?.auth_required) signedOut();
  if (!r.ok) throw new Error((j && j.detail) || r.statusText || "Upload failed");
  return j;
}

/** A notice under the header while saving fails. `storage` comes with every status message. */
export function SaveNotice({ storage }: { storage: any }) {
  if (!storage?.saving_failed) return null;
  return (
    <div className="notice bad wide" role="alert" data-testid="save-failed">
      Your changes are not being saved: {storage.error}. PaperDesk keeps running and retries; anything done since
      {storage.last_save ? ` ${dateTime(storage.last_save)}` : " the start"} is lost if it stops before saving works again.
    </div>
  );
}

function RestorePreview({ preview, busy, onApply, onCancel }: { preview: any; busy: boolean; onApply: () => void; onCancel: () => void }) {
  const changed = preview.accounts.filter((a: any) => a.change !== "unchanged");
  return (
    <div className="data-preview" role="region" aria-label="What the restore changes">
      <p><b>Restore {preview.source}</b>{preview.created ? `, made ${dateTime(Date.parse(preview.created) / 1000)}` : ""}.
        Nothing has changed yet.</p>
      {preview.migrated_from && <p className="muted">It is in an older format (version {preview.migrated_from}) and will be converted.</p>}
      <ul className="data-changes">
        {preview.accounts.map((a: any) => (
          <li key={a.id} data-change={a.change}>
            <b>{a.name}</b> <span className="muted">({a.id})</span>: {a.change === "added" ? "added" : a.change === "removed" ? "removed" : a.change === "replaced" ? "replaced" : "unchanged"}
            {a.after && a.change !== "unchanged" && <span className="muted"> · cash {usd(Number(a.after.cash))}, {a.after.positions} positions, {a.after.fills} fills</span>}
          </li>
        ))}
      </ul>
      <p className="muted">Open orders {preview.orders.before} → {preview.orders.after} · watchlists {preview.watchlists.before} → {preview.watchlists.after} · alerts {preview.alerts.before} → {preview.alerts.after}</p>
      {preview.missing_symbols?.length > 0 && (
        <div className="notice" role="status">These symbols are not in the current market data feed: {preview.missing_symbols.join(", ")}. Their positions stay at cost and cannot be priced or traded here.</div>
      )}
      <p className="muted">{changed.length ? "Everything you have now is backed up first, so this can be undone from the backup list." : "The accounts are the same as now."}</p>
      <div className="row">
        <button className="ghost" onClick={onCancel} disabled={busy}>Cancel</button>
        <button className="primary" onClick={onApply} disabled={busy} data-testid="restore-apply">Restore</button>
      </div>
    </div>
  );
}

/** The Data and backups dialog (topActions → "Data"). */
export function DataDialog({ accountId, onClose, notify, onRestored }: {
  accountId: string; onClose: () => void; notify: (msg: string, bad?: boolean) => void; onRestored: () => void;
}) {
  const [st, setSt] = useState<any>(null);
  const [rows, setRows] = useState<Backup[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<any>(null);
  const [imp, setImp] = useState<any>(null);
  const [impName, setImpName] = useState("");
  const file = useRef<HTMLInputElement>(null);
  const acctFile = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const s = await api("/data/status");
      setSt(s);
      if (s.enabled) setRows(await api("/data/backups"));
    } catch (e: any) { setError(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await fn(); } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  const backupNow = () => run(async () => { const r = await api("/data/backups", { method: "POST" }); notify(`Backup saved (${size(r.size)})`); await load(); });
  const previewBackup = (name: string) => run(async () => { setImp(null); setPreview(await api("/data/restore/preview", { method: "POST", body: { backup: name } })); });
  const previewFile = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f) run(async () => { setImp(null); setPreview(await upload("/data/restore/preview", f, "application/zip")); });
  };
  const apply = () => run(async () => {
    await api("/data/restore/apply", { method: "POST", body: { token: preview.token } });
    setPreview(null); notify("Backup restored"); onRestored(); await load();
  });
  const importFile = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f) run(async () => { setPreview(null); const p = await upload("/data/accounts/import/preview", f, "application/json"); setImp(p); setImpName(p.name); });
  };
  const applyImport = () => run(async () => {
    const r = await api("/data/accounts/import/apply", { method: "POST", body: { token: imp.token, name: impName } });
    setImp(null); notify(`Account imported as ${r.account_id}`); onRestored();
  });
  const del = (name: string) => run(async () => { await api(`/data/backups/${encodeURIComponent(name)}`, { method: "DELETE" }); await load(); });

  const save = st?.save;
  const replay = st?.mode === "replay";
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal data-modal" role="dialog" aria-modal="true" aria-labelledby="data-title" onClick={(e) => e.stopPropagation()}>
        <h3 id="data-title">Data and backups</h3>
        {!st && !error && <p className="muted">Loading…</p>}
        {st && !st.enabled && <p className="muted">This server was started without a data folder, so nothing is saved.</p>}
        {st?.enabled && (
          <section className="data-status" aria-label="Save status" data-testid="save-status">
            {save?.saving_failed
              ? <div className="notice bad" role="alert">Saving fails: {save.error}. Tried {save.attempts} time{save.attempts === 1 ? "" : "s"}; next try {save.next_retry ? dateTime(save.next_retry) : "soon"}.</div>
              : <p>Last saved {save?.last_save ? dateTime(save.last_save) : "not yet in this run"}. Saving works.</p>}
            <p className="muted small">Folder {st.data_dir} · format version {st.schema_version}
              {st.backups.interval_seconds > 0 ? ` · automatic backup every ${Math.round(st.backups.interval_seconds / 60)} min (keeps ${st.backups.keep_hourly} hourly and ${st.backups.keep_daily} daily)` : " · automatic backups are off"}</p>
            {st.backups.last_error && <div className="notice bad" role="alert">The last backup failed: {st.backups.last_error}</div>}
            {save?.warnings?.length > 0 && <div className="notice" role="status">{save.warnings.join(" ")}</div>}
          </section>
        )}
        {replay && <div className="notice" role="status">A replay is running. Backups and restores work on your own accounts, which are put aside until you exit the replay.</div>}
        {st?.enabled && (
          <div className="data-actions">
            <button className="primary" onClick={backupNow} disabled={busy || replay}>Back up now</button>
            <a className={"button ghost" + (replay ? " disabled" : "")} href="/api/data/export" download data-testid="download-all"
              aria-disabled={replay} onClick={(e) => replay && e.preventDefault()}>Download everything</a>
            <button className="ghost" onClick={() => file.current?.click()} disabled={busy || replay}>Restore from a file…</button>
            <input ref={file} type="file" accept=".zip,application/zip" hidden onChange={previewFile} data-testid="restore-file" />
          </div>
        )}
        {preview && <RestorePreview preview={preview} busy={busy} onApply={apply} onCancel={() => setPreview(null)} />}
        {st?.enabled && (
          <section aria-label="Backups">
            <h4>Backups</h4>
            {rows.length === 0 && <p className="muted">No backups yet.</p>}
            <ul className="data-list" data-testid="backup-list">
              {rows.map((b) => (
                <li key={b.name}>
                  <span className="data-when"><b>{dateTime(b.created_ts)}</b><small className="muted">{KIND[b.kind] || b.kind} · {size(b.size)}</small></span>
                  <span className="data-buttons">
                    <a className="button ghost sm" href={`/api/data/backups/${encodeURIComponent(b.name)}`} download={b.name}>Download</a>
                    <button className="ghost sm" onClick={() => previewBackup(b.name)} disabled={busy || replay}>Restore…</button>
                    {b.kind === "manual" && <button className="ghost sm" onClick={() => del(b.name)} disabled={busy} aria-label={`Delete backup ${b.name}`}>Delete</button>}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}
        <section aria-label="Move an account">
          <h4>Move one account</h4>
          <p className="muted small">Export an account to a file and import it on another PaperDesk or in another data mode. It gets new ids there.</p>
          <div className="data-actions">
            {accountId && <a className="button ghost" href={`/api/data/accounts/${encodeURIComponent(accountId)}/export`} download>Export this account</a>}
            <button className="ghost" onClick={() => acctFile.current?.click()} disabled={busy || replay}>Import an account…</button>
            <input ref={acctFile} type="file" accept=".json,application/json" hidden onChange={importFile} data-testid="import-file" />
          </div>
          {imp && (
            <div className="data-preview" role="region" aria-label="What the import adds">
              <p>Adds <b>{imp.type}</b> account as {imp.new_id}: cash {usd(Number(imp.cash))}, {imp.positions} positions, {imp.fills} fills, {imp.active_orders} open orders.</p>
              {imp.missing_symbols.length > 0 && <div className="notice" role="status">Not in this feed: {imp.missing_symbols.join(", ")}. Those positions stay at cost and cannot be priced or traded here{imp.canceled_orders ? `; ${imp.canceled_orders} open order${imp.canceled_orders === 1 ? " is" : "s are"} canceled` : ""}.</div>}
              <label>Name<input value={impName} onChange={(e) => setImpName(e.target.value)} maxLength={40} /></label>
              <div className="row">
                <button className="ghost" onClick={() => setImp(null)} disabled={busy}>Cancel</button>
                <button className="primary" onClick={applyImport} disabled={busy}>Import</button>
              </div>
            </div>
          )}
        </section>
        {error && <div className="notice bad" role="alert">{error}</div>}
        <div className="row"><button className="ghost" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}

/** Shown instead of the app when the saved state could not be read or is from a newer PaperDesk. */
function RecoveryScreen({ recovery, onDone }: { recovery: Recovery; onDone: () => void }) {
  const [rows, setRows] = useState<Backup[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [quit, setQuit] = useState(false);
  const [confirmFresh, setConfirmFresh] = useState(false);
  useEffect(() => { api("/data/recovery").then((r) => setRows(r.backups || [])).catch((e) => setError(e.message)); }, []);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await fn(); } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  const restore = (name?: string) => run(async () => { await api("/data/recovery/restore", { method: "POST", body: name ? { backup: name } : {} }); onDone(); });
  const fresh = () => run(async () => { await api("/data/recovery/fresh", { method: "POST" }); onDone(); });
  const stop = () => run(async () => { await api("/data/recovery/quit", { method: "POST" }); setQuit(true); });
  if (quit) return <div className="recovery"><div className="recovery-card"><h1>PaperDesk has stopped</h1><p>Nothing was changed. You can close this tab.</p></div></div>;
  const newer = recovery.kind === "newer";
  return (
    <div className="recovery" role="main">
      <div className="recovery-card" data-testid="recovery">
        <h1>{newer ? "Saved data is from a newer PaperDesk" : "Saved data could not be read"}</h1>
        <p>{recovery.reason}.</p>
        <p className="muted">{newer
          ? "This version cannot read it without risking losing what the newer version stored, so it has not touched the file. Update PaperDesk, or choose below."
          : "Nothing has been overwritten."}{recovery.copy ? ` A copy of the files is kept in ${recovery.copy}.`
            : " No copy of the damaged files could be made yet (the disk may be full). Before restoring or starting fresh, PaperDesk copies or moves them aside, and does neither if it cannot."}</p>
        <div className="recovery-choices">
          <button className="primary" disabled={busy || rows.length === 0} onClick={() => restore()}>
            {rows.length ? `Restore the latest backup (${dateTime(rows[0].created_ts)})` : "No backups to restore"}
          </button>
          {!confirmFresh
            ? <button className="ghost" disabled={busy} onClick={() => setConfirmFresh(true)}>Start fresh…</button>
            : <button className="ghost danger" disabled={busy} onClick={fresh}>Start fresh with new paper accounts</button>}
          <button className="ghost" disabled={busy} onClick={stop}>Quit without changing anything</button>
        </div>
        {rows.length > 1 && (
          <details>
            <summary>Choose another backup</summary>
            <ul className="data-list">
              {rows.map((b) => (
                <li key={b.name}>
                  <span className="data-when"><b>{dateTime(b.created_ts)}</b><small className="muted">{KIND[b.kind] || b.kind} · {size(b.size)}</small></span>
                  <button className="ghost sm" disabled={busy} onClick={() => restore(b.name)}>Restore</button>
                </li>
              ))}
            </ul>
          </details>
        )}
        {error && <div className="notice bad" role="alert">{error}</div>}
      </div>
    </div>
  );
}

/** Renders the recovery screen instead of the app while the data folder is in recovery. */
export function RecoveryGate({ children }: { children: ReactNode }) {
  const [rec, setRec] = useState<Recovery | null | undefined>(undefined);
  const [gen, setGen] = useState(0);
  useEffect(() => {
    api("/data/status").then((s) => setRec(s.recovery || null)).catch(() => setRec(null));
  }, [gen]);
  if (rec === undefined) return null;
  if (rec) return <RecoveryScreen recovery={rec} onDone={() => { setRec(undefined); setGen((g) => g + 1); }} />;
  return <>{children}</>;
}
