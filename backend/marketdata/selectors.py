"""
Market data reads (architecture rule 1).

TODO Phase 2:
    latest_prices(tickers) -> dict[str, Decimal]        # newest PriceSnapshot per ticker
    price_history_frame(tickers, days=252) -> DataFrame # wide frame, one column per ticker,
                                                        # indexed by date and inner-joined so
                                                        # the risk engine gets aligned rows
    missing_tickers(tickers) -> list[str]               # nothing stored yet
"""
