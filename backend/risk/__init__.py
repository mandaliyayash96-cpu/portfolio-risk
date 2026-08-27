"""
Risk mathematics.

`engine.py` is pure (NumPy/pandas/SciPy only) and must stay importable without
Django. This package is therefore NOT a Django app yet - nothing here needs the
app registry.

TODO Phase 4: services.py reads settings, loads holdings + price history via
              selectors, and calls engine.build_report.
TODO Phase 5: optimizer.py - min_variance_weights(cov), efficient_frontier(...).
"""
