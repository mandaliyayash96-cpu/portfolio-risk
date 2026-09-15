"""
AI insights: the risk report, explained in plain English by a language model.

WHAT THIS IS AND IS NOT
-----------------------
It is a DESCRIPTION of numbers this backend already computed. It is not advice,
not a forecast, and not a second opinion on the maths. The model is handed the
holdings and the metrics `risk.services.compute_risk` produced and asked to
say, in a beginner's words, what stands out: which positions are up or down,
how concentrated the book is, how rough the ride has been. Nothing is
recomputed here - the figures in the prompt are the figures on the dashboard,
so the two cannot disagree.

THREE LAYERS BETWEEN THE MODEL AND FINANCIAL ADVICE
---------------------------------------------------
1. THE PROMPT (`build_prompt`) says observations only, forbids buy/sell/
   "you should", forbids predictions, and lists the allowed categories.
2. THE RESPONSE SCHEMA (`gemini.RESPONSE_SCHEMA`) pins the output to
   {category ∈ five values, text}, so the model cannot add a "recommendation"
   field or wander off into prose.
3. THE FILTER (`_is_advice`) runs on every insight before it leaves this
   module and drops any that reads as a directive or a prediction. The prompt
   is a request to the model; the filter is the guarantee. A dropped insight
   costs the user one sentence; a shipped one costs the product its footing.

The disclaimer is appended HERE, by code, on every response including the
failure ones. It is not something the model is asked to remember.

FAILURE IS A RESPONSE, NOT AN EXCEPTION
---------------------------------------
Every GeminiError - no key, a 429 from the free tier, a timeout, an answer that
was not the JSON we asked for - becomes `{"available": false, ...}` with a
sentence the panel can show and a `reason` it can phrase. HTTP 200, no
traceback, and the risk dashboard around it never notices. The ONLY errors
that propagate are the data ones `compute_risk` raises (empty portfolio, too
little history), because those are not this feature's fault and the client
already knows their codes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone

from insights import gemini
from portfolio.selectors import get_holdings
from risk.services import compute_risk

logger = logging.getLogger(__name__)

#: Appended by code to every response. The model never sees this sentence and
#: is never asked to produce it - it is a statement about the product.
DISCLAIMER = "Insights are informational only, not financial advice."

#: What the panel shows when the model could not be reached. One sentence, and
#: the same one for every reason, because "try again" is the only fix the user
#: has for any of them.
UNAVAILABLE_MESSAGE = "AI insights unavailable right now, try again."

#: The model is asked for 3-6; anything past this is cut, not shown.
MAX_INSIGHTS = 6

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# The advice filter
#
# Word-boundaried, case-insensitive. Two kinds of pattern: DIRECTIVES (telling
# the reader to do something with a holding) and PREDICTIONS (telling them
# what a price will do). Past-tense trade words - "bought at", "sold" - are
# left alone; they describe the data, which is the whole job.
#
# `buy` needs care because the cost basis is naturally called the "buy price".
# The prompt calls it "avg cost" so the model has no reason to say "buy", and
# the pattern excuses "average buy price" / "buy date" in case it does anyway.
# ---------------------------------------------------------------------------
_ADVICE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Directives.
        r"\byou (?:should|ought to|need to|must|could|might want to|may want to)\b",
        r"\b(?:we|i) (?:recommend|suggest|advise)\b",
        r"\brecommend(?:ed|s|ation|ations)?\b",
        r"\badvis(?:e|es|ed|able)\b",
        r"\bconsider (?:buying|selling|adding|reducing|trimming|exiting|"
        r"cutting|increasing|decreasing|rebalancing|diversifying|moving|"
        r"switching|shifting|taking)\b",
        r"\b(?:should|must) (?:be )?(?:buy|sell|add|reduce|trim|exit|hold|held|"
        r"increase|decrease|rebalance|diversify|move|switch|shift|cut)(?:ed|d)?\b",
        r"\b(?:it is|it's|now is|this is) (?:a good |the )?time to\b",
        r"(?<!average )\bbuy\b(?!\s+(?:price|date|cost))",
        r"\b(?:buys|buying|purchase|purchasing)\b",
        r"\bsell(?:s|ing)?\b(?!-off)",
        r"\b(?:dump|offload|accumulate|go long|go short)\b\s+(?:this|that|the|your|it|them|[A-Z])",
        # Predictions.
        r"\bwill (?:likely |probably |soon )?(?:rise|fall|drop|climb|rally|"
        r"recover|rebound|crash|soar|plunge|outperform|underperform|"
        r"go up|go down|continue|keep)\b",
        r"\b(?:is|are) (?:likely|expected|going|set|poised) to (?:rise|fall|"
        r"drop|climb|rally|recover|rebound|crash|soar|plunge|outperform|"
        r"underperform|go up|go down|continue)\b",
        r"\bprice target\b",
        r"\bexpect(?:ed|s)? (?:it |them )?to (?:rise|fall|drop|climb|rally|"
        r"recover|rebound|outperform|underperform)\b",
    )
]


def _is_advice(text: str) -> bool:
    """True when the sentence reads as a directive or a forecast."""
    return any(pattern.search(text) for pattern in _ADVICE_PATTERNS)


# ---------------------------------------------------------------------------
# Gathering - from the existing services, never recomputed
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _HoldingView:
    """One measured position, with the cost basis the risk report leaves out."""

    ticker: str
    quantity: Decimal
    avg_cost: Decimal | None  # None when the holding row has vanished mid-request
    price: Decimal
    price_source: str
    market_value: Decimal
    weight: float
    volatility: float | None

    @property
    def cost_basis(self) -> Decimal | None:
        return None if self.avg_cost is None else self.quantity * self.avg_cost

    @property
    def unrealised_pl(self) -> Decimal | None:
        cost = self.cost_basis
        return None if cost is None else self.market_value - cost

    @property
    def unrealised_pl_pct(self) -> float | None:
        """Percent, e.g. 12.5 for +12.5%. None when there is no cost to compare to."""
        cost = self.cost_basis
        if cost is None or cost <= ZERO:
            return None
        return float((self.market_value - cost) / cost * 100)


@dataclass(frozen=True)
class _Snapshot:
    """Everything the prompt needs, and everything the response echoes."""

    portfolio_id: int
    name: str
    currency: str
    holdings: list[_HoldingView]
    excluded: list[dict]
    market_value: Decimal
    report: dict  # the compute_risk dict, for the metrics block

    @property
    def cost_basis(self) -> Decimal:
        return sum((h.cost_basis for h in self.holdings if h.cost_basis is not None), ZERO)

    @property
    def unrealised_pl(self) -> Decimal:
        return sum((h.unrealised_pl for h in self.holdings if h.unrealised_pl is not None), ZERO)

    @property
    def unrealised_pl_pct(self) -> float | None:
        cost = self.cost_basis
        return None if cost <= ZERO else float(self.unrealised_pl / cost * 100)


def gather(portfolio_id: int) -> _Snapshot:
    """
    The report the dashboard already shows, plus each holding's cost basis.

    `compute_risk` is called exactly as /api/risk/ calls it, so the valuation,
    the weights and every metric are the ones on screen. The one thing that
    block does not carry is `avg_buy_price` - it describes a VALUATION, not a
    row - so that is joined in from the holdings themselves by ticker.

    Raises whatever `compute_risk` raises: NotFoundError, EmptyPortfolioError,
    InsufficientHistoryError. Those are the portfolio's problems, not the
    model's, and the client already renders their codes.
    """
    report = compute_risk(portfolio_id)
    block = report["portfolio"]

    costs = {
        (holding.ticker or "").strip().upper(): holding.avg_buy_price
        for holding in get_holdings(portfolio_id)
    }
    per_asset_vol = report.get("per_asset_volatility") or {}

    holdings = [
        _HoldingView(
            ticker=row["ticker"],
            quantity=Decimal(row["quantity"]),
            avg_cost=costs.get(row["ticker"]),
            price=Decimal(row["price"]),
            price_source=row["price_source"],
            market_value=Decimal(row["market_value"]),
            weight=float(row["weight"]),
            volatility=per_asset_vol.get(row["ticker"]),
        )
        for row in block["holdings"]
    ]

    return _Snapshot(
        portfolio_id=block["id"],
        name=block["name"],
        currency=block["base_currency"],
        holdings=holdings,
        excluded=list(block.get("excluded") or []),
        market_value=Decimal(block["market_value"]),
        report=report,
    )


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------
def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _signed_money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{'+' if value >= ZERO else '-'}{abs(value):,.2f}"


def _pct(value: float | None, digits: int = 1) -> str:
    """A FRACTION (0.175) as a percentage string ("17.5%")."""
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def _signed_pct_points(value: float | None) -> str:
    """Already in percent (12.5) -> "+12.5%"."""
    if value is None:
        return "n/a"
    return f"{'+' if value >= 0 else '-'}{abs(value):.1f}%"


def _ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def build_prompt(snapshot: _Snapshot) -> str:
    """
    The instruction block followed by the data block. A pure function of the
    snapshot, so the tests can assert on exactly what the model is told.

    The rules come FIRST and are numbered, because a model reads top-down and
    the constraint that matters most - observations only - has to be in place
    before it sees a single number it might be tempted to act on.

    Cost basis is called "avg cost", not "buy price", so the model has no
    natural reason to use a word the advice filter watches for.
    """
    report = snapshot.report
    lines: list[str] = []

    lines.append(
        "You are an assistant that explains portfolio data to a beginner investor "
        "in plain English."
    )
    lines.append("")
    lines.append("STRICT RULES - read these before the data:")
    lines.append(
        "1. OBSERVATIONS ONLY. Describe what the numbers show. You are not a "
        "financial adviser and must not give financial advice."
    )
    lines.append(
        "2. Never tell the reader to buy, sell, hold, add, trim, reduce, exit or "
        'rebalance any holding. Do not use the words "buy", "sell", "you should", '
        '"consider", "recommend" or "advise".'
    )
    lines.append(
        "3. Do not predict prices or returns. Say nothing about what any holding "
        '"will", "may" or "is likely to" do next.'
    )
    lines.append(
        "4. Use only the figures below. Do not invent holdings, prices, news, or "
        "reasons for a move."
    )
    lines.append(
        "5. Plain language a beginner understands. If you use a term like "
        "volatility or drawdown, explain it in a few words. At most two "
        "sentences per insight."
    )
    lines.append(
        "6. Where a figure is notable, say so and say why it is worth attention. "
        "That is as far as you go."
    )
    lines.append("")
    lines.append(
        f"TASK: Write 3 to {MAX_INSIGHTS} concise insights about this portfolio. "
        "Cover, where the data supports it:"
    )
    lines.append('  - notable gains (category "gain")')
    lines.append('  - notable losses or drawdowns (category "loss")')
    lines.append('  - concentration and the overall risk level (category "risk")')
    lines.append('  - how diversified the holdings are (category "diversification")')
    lines.append('  - anything else worth knowing (category "general")')
    lines.append("Each insight names the specific holding or figure it is about.")
    lines.append("")

    # -- data ---------------------------------------------------------------
    currency = snapshot.currency
    lines.append(f'PORTFOLIO: "{snapshot.name}" ({currency}).')
    lines.append(
        f"Measured value: {_money(snapshot.market_value)}. "
        f"Avg cost of those positions: {_money(snapshot.cost_basis)}. "
        f"Unrealised P/L: {_signed_money(snapshot.unrealised_pl)} "
        f"({_signed_pct_points(snapshot.unrealised_pl_pct)})."
    )
    lines.append(
        f"Window: {str(report.get('start', ''))[:10]} to {str(report.get('end', ''))[:10]}, "
        f"{report.get('observations', 0)} daily observations."
    )
    lines.append("")

    lines.append("HOLDINGS (weight = share of measured value):")
    for h in snapshot.holdings:
        lines.append(
            f"  - {h.ticker}: {h.quantity.normalize():f} units, "
            f"avg cost {_money(h.avg_cost)}, price {_money(h.price)} ({h.price_source}), "
            f"value {_money(h.market_value)}, weight {_pct(h.weight)}, "
            f"P/L {_signed_money(h.unrealised_pl)} ({_signed_pct_points(h.unrealised_pl_pct)}), "
            f"ann. volatility {_pct(h.volatility)}"
        )
    lines.append("")

    if snapshot.excluded:
        lines.append(
            "EXCLUDED (no usable price data - NOT valued and NOT in any figure "
            "above; say only that they could not be measured):"
        )
        for entry in snapshot.excluded:
            units = Decimal(str(entry["quantity"])).normalize()
            lines.append(f"  - {entry['ticker']}: {units:f} units - {entry['detail']}")
        lines.append("")

    benchmark = (report.get("benchmark") or {}).get("ticker") or "the benchmark"
    confidence = int(round(float((report.get("params") or {}).get("confidence", 0.95)) * 100))
    hhi = report.get("hhi")
    lines.append("RISK METRICS (annualised unless stated):")
    lines.append(f"  - Volatility: {_pct(report.get('annualized_volatility'))}")
    lines.append(f"  - Annualised return over the window: {_pct(report.get('annualized_return'))}")
    lines.append(f"  - Beta vs {benchmark}: {_ratio(report.get('beta'))}")
    lines.append(f"  - Sharpe ratio: {_ratio(report.get('sharpe'))}")
    lines.append(f"  - Sortino ratio: {_ratio(report.get('sortino'))}")
    lines.append(f"  - Max drawdown: {_pct(report.get('max_drawdown'))}")
    lines.append(
        f"  - 1-day VaR ({confidence}%, historical): {_pct(report.get('var_historical'))}; "
        f"CVaR: {_pct(report.get('cvar'))}"
    )
    lines.append(
        f"  - Concentration (HHI): {_ratio(hhi)} "
        f"(effective holdings: {_ratio(report.get('effective_holdings'))}). "
        "HHI is 1.0 for a single holding and 1/N for N equal weights."
    )
    lines.append("")
    lines.append('Respond as JSON: {"insights": [{"category": "...", "text": "..."}]}')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parsing and filtering the answer
# ---------------------------------------------------------------------------
def parse_insights(raw: str) -> list[dict]:
    """
    The model's JSON -> [{"category", "text"}, ...], validated and filtered.

    Strict about shape (the schema was pinned, so anything else is a fault),
    tolerant about content (a bad category becomes "general"; an empty text is
    skipped; an insight that reads as advice is dropped and counted).

    Raises:
        gemini.GeminiBadResponse: not JSON, or not the object we asked for.
    """
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise gemini.GeminiBadResponse("The model's answer was not valid JSON.") from exc

    items = payload.get("insights") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise gemini.GeminiBadResponse("The model's answer had no insights list.")

    kept: list[dict] = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if _is_advice(text):
            dropped += 1
            continue
        category = str(item.get("category") or "").strip().lower()
        if category not in gemini.CATEGORIES:
            category = "general"
        kept.append({"category": category, "text": text})
        if len(kept) >= MAX_INSIGHTS:
            break

    if dropped:
        # Count only. The sentence itself is the model's, not the user's, but
        # keeping model output out of the logs is the cheaper habit.
        logger.info("Dropped %d AI insight(s) that read as advice.", dropped)
    return kept


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------
def build_ai_insights(portfolio_id: int) -> dict:
    """
    Plain-English insights for one portfolio, or a clean "not right now".

    Returns:
        On success:
            {"available": true, "insights": [{"category", "text"}, ...],
             "model": "...", "generated_at": ISO-8601,
             "portfolio": {"id", "name", "market_value"},
             "excluded": [...as the risk report lists them...],
             "disclaimer": DISCLAIMER}

        When the model could not be used:
            {"available": false, "insights": [], "reason": "rate_limited" |
             "unavailable" | "not_configured" | "bad_response",
             "message": UNAVAILABLE_MESSAGE, "portfolio": {...},
             "excluded": [...], "disclaimer": DISCLAIMER}

        Both are HTTP 200. `insights` may be an empty list on success too, if
        every sentence the model wrote was filtered - the panel shows an empty
        state and the user tries again.

    Raises:
        Only what `compute_risk` raises about the DATA - not found, empty
        portfolio, insufficient history. Nothing about Gemini ever propagates.
    """
    snapshot = gather(portfolio_id)
    prompt = build_prompt(snapshot)

    try:
        raw = gemini.generate(prompt)
        insights = parse_insights(raw)
    except gemini.GeminiError as exc:
        # Reason only. exc's message is ours, but the habit of not echoing an
        # upstream failure into a log line is what keeps this file safe to
        # grep.
        logger.warning("AI insights unavailable for portfolio %s: %s", portfolio_id, exc.reason)
        return {
            **_envelope_common(snapshot),
            "available": False,
            "insights": [],
            "reason": exc.reason,
            "message": UNAVAILABLE_MESSAGE,
        }

    return {
        **_envelope_common(snapshot),
        "available": True,
        "insights": insights,
        "model": gemini.model_name(),
        "generated_at": timezone.now().isoformat(),
    }


def _envelope_common(snapshot: _Snapshot) -> dict:
    """The keys every response carries, success or not."""
    return {
        "portfolio": {
            "id": snapshot.portfolio_id,
            "name": snapshot.name,
            "base_currency": snapshot.currency,
            # A string, like every money figure this API emits (common/MONEY.md).
            "market_value": str(snapshot.market_value),
        },
        # Dead tickers travel with the insights so the panel can say "these
        # were not part of the analysis" without a second request.
        "excluded": snapshot.excluded,
        "disclaimer": DISCLAIMER,
    }
