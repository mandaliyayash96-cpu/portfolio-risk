"""
Market data writes (architecture rule 1).

TODO Phase 2:
    provider.py            -> MarketDataProvider ABC: get_live_prices(tickers),
                              get_history(tickers, start, end) -> DataFrame
    providers/yfinance_provider.py -> the only module in the codebase allowed to
                              import yfinance (architecture rule 4)
    get_provider()         -> reads settings, returns the configured implementation
    fetch_live(tickers)    -> provider call -> bulk_create PriceSnapshot rows
    fetch_history(tickers, days=252) -> provider call -> bulk upsert PriceHistory
    tasks.py               -> Celery: poll_prices() run by Beat every 60s
"""
