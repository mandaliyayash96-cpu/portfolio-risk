"""
Risk mathematics.

`engine.py` is pure (NumPy/pandas/SciPy only) and must stay importable without
Django - `tests/test_engine.py::TestEnginePurity` enforces it, so nothing may
be imported at this package level.

    engine.py    pure maths, no Django (architecture rule 2)
    services.py  ORM + settings + engine, the only impure layer
    views.py     thin DRF view over services

TODO Phase 5: optimizer.py - min_variance_weights(cov), efficient_frontier(...).
"""
