"""Reports and analytics over an Account and price history.

Every function here is pure: it reads an Account (or plain rows) and returns new values without
changing anything. Money stays Decimal; ratios (returns, volatility, Sharpe) are Decimal too.
"""
