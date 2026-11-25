/** Persistent notice when Alpaca was configured but could not be used at start-up: the app is running on
 * simulated data and the simulated accounts, and the live-data accounts are not loaded (nor changed). */
export function FeedNotice({ status }: { status: any }) {
  const cause = status?.live_unavailable;
  if (!cause) return null;
  return (
    <div className="notice bad wide feed-notice" role="alert">
      Could not connect to Alpaca ({cause}). Showing simulated data and your simulated accounts. Your live-data
      accounts are untouched; restart PaperDesk to retry.
    </div>
  );
}
