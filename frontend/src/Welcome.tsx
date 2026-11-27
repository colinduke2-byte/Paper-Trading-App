// First-run card: what PaperDesk is, that the data is simulated, and a one-tap first trade.
export function Welcome({ simulated, symbol, onFirstTrade, onHelp, onDismiss }: {
  simulated: boolean; symbol: string; onFirstTrade: () => void; onHelp: () => void; onDismiss: () => void;
}) {
  return (
    <section className="welcome" role="region" aria-labelledby="welcome-title">
      <div className="welcome-text">
        <h2 id="welcome-title">Welcome to PaperDesk</h2>
        <p>
          PaperDesk lets you practice trading stocks and options with pretend money; nothing you do here touches a real
          broker. {simulated
            ? "The companies and their prices are simulated, so nothing here is real market data or advice."
            : "Prices come from the market data provider shown in the banner; the money and the orders are still pretend."}{" "}
          Start with a small trade, then watch it in Positions at the bottom.
        </p>
      </div>
      <div className="welcome-actions">
        <button className="primary" onClick={onFirstTrade}>Buy 10 shares of {symbol}</button>
        <button className="ghost" onClick={onHelp}>How PaperDesk works</button>
        <button className="ghost" onClick={onDismiss}>Dismiss</button>
      </div>
    </section>
  );
}
