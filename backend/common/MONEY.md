# Money & quantity math contract

Architecture rule 5: pick one representation and document it. This is it.

## The rule

| Layer | Type | Why |
|---|---|---|
| Models / DB columns | `Decimal` (`DecimalField`) | Exact. Cost basis, P&L and realised gains must not drift. |
| services.py / selectors.py | `Decimal` | Stays exact right up to the analytics boundary. |
| `risk/engine.py`, NumPy/Pandas | `float64` | Vectorised math; NumPy cannot use Decimal without collapsing to object arrays. |
| API responses (JSON) | `str` for money, `float` for ratios | DRF serialises `DecimalField` as a string by default — no precision lost in transit. |

**Conversion happens exactly once**, at the pandas boundary in
`marketdata/selectors.py::get_history_df`, which reads Decimal closes out of the
DB and emits a `float64` column. `risk/services.py` then builds the weights
vector `w` and the returns matrix `R` from frames that are already float64.
Nothing converts back, because risk outputs (volatility, VaR, Sharpe, beta) are
ratios and statistics, not money.

- No `float` is ever written to a money column.
- No `Decimal` is ever handed to NumPy.
- `engine.py` imports neither Django nor Decimal (architecture rule 2).

## Field precisions

Defined once in `common/models.py` and imported everywhere:

- `MONEY_MAX_DIGITS = 18`, `MONEY_DECIMAL_PLACES = 4` — prices, thresholds, metric values.
- `QUANTITY_MAX_DIGITS = 20`, `QUANTITY_DECIMAL_PLACES = 6` — share and MF unit counts.

## Rounding

Round only at the presentation edge, never mid-calculation. When a rounded
Decimal is required, use `quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)`.

## Currency

`Portfolio.base_currency` defaults to `INR`. Phase 1 assumes every holding in a
portfolio is quoted in that currency — there is no FX conversion anywhere.

> **TODO Phase 5+:** if multi-currency holdings are ever supported, add an FX
> rate table and convert to `base_currency` inside services, before weights are
> computed. The risk engine must keep seeing a single-currency value vector.
