"""
Tests for insights/services.py - the prompt, the parser, the filter, and the
graceful-failure contract. No network: `gemini.generate` is always patched.
"""

import json
from decimal import Decimal

import pytest

from insights import gemini
from insights.services import (
    DISCLAIMER,
    MAX_INSIGHTS,
    UNAVAILABLE_MESSAGE,
    _is_advice,
    build_ai_insights,
    build_prompt,
    gather,
    parse_insights,
)

from .conftest import SAMPLE_INSIGHTS, gemini_json

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Gathering: from the existing services, with the cost basis joined in
# ---------------------------------------------------------------------------
class TestGather:
    def test_reuses_the_risk_reports_valuation(self, my_funded_portfolio):
        from risk.services import compute_risk

        snapshot = gather(my_funded_portfolio.pk)
        report = compute_risk(my_funded_portfolio.pk)

        assert str(snapshot.market_value) == report["portfolio"]["market_value"]
        assert [h.ticker for h in snapshot.holdings] == report["tickers"]
        for holding in snapshot.holdings:
            assert holding.weight == pytest.approx(report["weights"][holding.ticker])

    def test_joins_avg_cost_and_computes_unrealised_pl(self, my_funded_portfolio):
        snapshot = gather(my_funded_portfolio.pk)
        by_ticker = {h.ticker: h for h in snapshot.holdings}

        reliance = by_ticker["RELIANCE.NS"]
        assert str(reliance.avg_cost) == "800.0000"
        assert reliance.unrealised_pl == Decimal("20000")
        assert reliance.unrealised_pl_pct == pytest.approx(25.0)

        tcs = by_ticker["TCS.NS"]
        assert tcs.unrealised_pl == Decimal("-10000")
        assert tcs.unrealised_pl_pct == pytest.approx(-9.0909, abs=1e-3)

        # Portfolio-level: 200,000 value on 190,000 cost.
        assert snapshot.cost_basis == Decimal("190000")
        assert snapshot.unrealised_pl == Decimal("10000")

    def test_dead_tickers_are_excluded_not_valued(self, with_dead_ticker):
        snapshot = gather(with_dead_ticker.pk)

        assert [h.ticker for h in snapshot.holdings] == ["RELIANCE.NS", "TCS.NS"]
        assert [e["ticker"] for e in snapshot.excluded] == ["DEADCO.NS"]
        assert snapshot.excluded[0]["reason"] == "no_price"
        # And its cost basis is NOT in the portfolio total.
        assert snapshot.cost_basis == Decimal("190000")


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------
class TestPrompt:
    def test_carries_every_holding_with_its_figures(self, my_funded_portfolio):
        prompt = build_prompt(gather(my_funded_portfolio.pk))

        assert "RELIANCE.NS: 100 units" in prompt
        assert "avg cost 800.00" in prompt
        assert "price 1,000.00 (live)" in prompt
        assert "value 100,000.00" in prompt
        assert "weight 50.0%" in prompt
        assert "P/L +20,000.00 (+25.0%)" in prompt

        assert "TCS.NS: 50 units" in prompt
        assert "P/L -10,000.00 (-9.1%)" in prompt

    def test_carries_the_portfolio_level_figures(self, my_funded_portfolio):
        prompt = build_prompt(gather(my_funded_portfolio.pk))

        assert "Measured value: 200,000.00" in prompt
        assert "Unrealised P/L: +10,000.00 (+5.3%)" in prompt
        assert "89 daily observations" in prompt

    def test_carries_the_risk_metrics(self, my_funded_portfolio):
        prompt = build_prompt(gather(my_funded_portfolio.pk))

        for label in (
            "Volatility:",
            "Annualised return over the window:",
            "Beta vs ^NSEI:",
            "Sharpe ratio:",
            "Sortino ratio:",
            "Max drawdown:",
            "1-day VaR (95%, historical):",
            "Concentration (HHI):",
        ):
            assert label in prompt, label
        # Metrics are real numbers, not the n/a placeholder.
        assert "Volatility: n/a" not in prompt
        assert "Beta vs ^NSEI: n/a" not in prompt

    def test_instructs_observations_only_and_no_advice(self, my_funded_portfolio):
        """The contract the feature rests on, spelled out to the model."""
        prompt = build_prompt(gather(my_funded_portfolio.pk))

        assert "OBSERVATIONS ONLY" in prompt
        assert "must not give financial advice" in prompt
        assert "Never tell the reader to buy, sell, hold" in prompt
        assert 'Do not use the words "buy", "sell", "you should"' in prompt
        assert "Do not predict prices or returns" in prompt
        assert "Use only the figures below" in prompt

    def test_rules_come_before_the_data(self, my_funded_portfolio):
        prompt = build_prompt(gather(my_funded_portfolio.pk))
        assert prompt.index("STRICT RULES") < prompt.index("PORTFOLIO:")
        assert prompt.index("PORTFOLIO:") < prompt.index("RISK METRICS")

    def test_names_every_allowed_category(self, my_funded_portfolio):
        prompt = build_prompt(gather(my_funded_portfolio.pk))
        for category in gemini.CATEGORIES:
            assert f'"{category}"' in prompt

    def test_dead_ticker_is_named_as_unmeasured_not_valued(self, with_dead_ticker):
        prompt = build_prompt(gather(with_dead_ticker.pk))

        assert "EXCLUDED" in prompt
        assert "DEADCO.NS: 20 units" in prompt
        assert "could not be valued" in prompt
        # It has no line in the HOLDINGS block, so no weight and no P/L.
        holdings_block = prompt[prompt.index("HOLDINGS"):prompt.index("EXCLUDED")]
        assert "DEADCO" not in holdings_block

    def test_no_excluded_block_when_nothing_was_excluded(self, my_funded_portfolio):
        assert "EXCLUDED" not in build_prompt(gather(my_funded_portfolio.pk))

    def test_the_api_key_is_never_in_the_prompt(self, my_funded_portfolio, gemini_settings):
        assert gemini_settings.GEMINI_API_KEY not in build_prompt(gather(my_funded_portfolio.pk))

    def test_the_word_buy_is_not_in_the_data_block(self, my_funded_portfolio):
        """
        The prompt says "avg cost", not "buy price", so the model has no reason
        to echo a word the advice filter would then drop the sentence for.
        """
        prompt = build_prompt(gather(my_funded_portfolio.pk))
        data_block = prompt[prompt.index("PORTFOLIO:"):]
        assert "buy" not in data_block.lower()


# ---------------------------------------------------------------------------
# Parsing and the advice filter
# ---------------------------------------------------------------------------
class TestParse:
    def test_well_formed_answer_round_trips(self):
        parsed = parse_insights(gemini_json())
        assert parsed == SAMPLE_INSIGHTS

    def test_unknown_category_becomes_general(self):
        parsed = parse_insights(gemini_json([{"category": "prophecy", "text": "The window is 89 days."}]))
        assert parsed == [{"category": "general", "text": "The window is 89 days."}]

    def test_category_is_case_insensitive(self):
        parsed = parse_insights(gemini_json([{"category": "GAIN", "text": "Up 20%."}]))
        assert parsed[0]["category"] == "gain"

    def test_empty_text_is_skipped(self):
        parsed = parse_insights(gemini_json([{"category": "gain", "text": "   "}]))
        assert parsed == []

    def test_capped_at_max_insights(self):
        many = [{"category": "general", "text": f"Fact {i}."} for i in range(MAX_INSIGHTS + 4)]
        assert len(parse_insights(gemini_json(many))) == MAX_INSIGHTS

    def test_not_json_is_a_bad_response(self):
        with pytest.raises(gemini.GeminiBadResponse):
            parse_insights("Here are some thoughts about your portfolio...")

    def test_json_without_an_insights_list_is_a_bad_response(self):
        with pytest.raises(gemini.GeminiBadResponse):
            parse_insights(json.dumps({"insights": "RELIANCE.NS is up."}))
        with pytest.raises(gemini.GeminiBadResponse):
            parse_insights(json.dumps(["RELIANCE.NS is up."]))

    def test_advice_is_dropped_and_the_rest_kept(self):
        answer = gemini_json(
            [
                {"category": "gain", "text": "RELIANCE.NS is up 25% on its average cost."},
                {"category": "risk", "text": "You should sell TCS.NS before it falls further."},
                {"category": "risk", "text": "Consider trimming RELIANCE.NS to cut concentration."},
                {"category": "loss", "text": "TCS.NS will likely recover next quarter."},
                {"category": "general", "text": "We recommend adding a bond fund."},
                {"category": "risk", "text": "Buy more RELIANCE.NS on dips."},
                {"category": "diversification", "text": "Two holdings at 50/50 means a fall in either moves half the book."},
            ]
        )
        parsed = parse_insights(answer)
        assert [p["text"][:12] for p in parsed] == ["RELIANCE.NS ", "Two holdings"]


class TestAdviceFilter:
    @pytest.mark.parametrize(
        "text",
        [
            "You should sell TCS.NS.",
            "you ought to reduce RELIANCE.NS",
            "You need to diversify.",
            "You might want to add gold.",
            "I recommend TCS.NS.",
            "This is a strong recommendation to hold.",
            "It is advisable to trim the position.",
            "Consider selling half of RELIANCE.NS.",
            "Consider diversifying into other sectors.",
            "The position should be reduced.",
            "Now is a good time to rebalance.",
            "It's time to exit TCS.NS.",
            "Buy RELIANCE.NS.",
            "Buying more here makes sense.",
            "Sell TCS.NS.",
            "Selling now would lock in the loss.",
            "TCS.NS will recover.",
            "RELIANCE.NS will likely outperform the index.",
            "TCS.NS is expected to rebound.",
            "The price target is 2,500.",
            "Accumulate RELIANCE.NS on dips.",
        ],
    )
    def test_directives_and_predictions_are_advice(self, text):
        assert _is_advice(text)

    @pytest.mark.parametrize(
        "text",
        [
            "RELIANCE.NS is up 25% on its average cost.",
            "RELIANCE.NS trades 25% above its average buy price.",
            "TCS.NS was bought at 2,200 and now trades at 2,000.",
            "The position was sold down by the market, not by you.",
            "A sell-off in IT pulled TCS.NS 8% below its peak.",
            "The portfolio holds two positions, so it is not very diversified.",
            "60% of the value sits in RELIANCE.NS, so its moves dominate the portfolio.",
            "The max drawdown of -12% means the portfolio fell 12% from its peak at one point.",
            "Volatility of 18% a year is moderate for an equity portfolio.",
            "The window is short (89 days), so the Sharpe ratio is noisy.",
            "Beta of 0.9 means the portfolio moved a little less than the NIFTY 50.",
            "DEADCO.NS could not be measured because it has no price data.",
        ],
    )
    def test_observations_are_not_advice(self, text):
        assert not _is_advice(text)


# ---------------------------------------------------------------------------
# The service: success and the graceful failures
# ---------------------------------------------------------------------------
class TestBuildAiInsights:
    def test_returns_parsed_insights_with_the_disclaimer(self, my_funded_portfolio, fake_gemini):
        with fake_gemini() as generate:
            result = build_ai_insights(my_funded_portfolio.pk)

        assert result["available"] is True
        assert result["insights"] == SAMPLE_INSIGHTS
        assert result["disclaimer"] == DISCLAIMER
        assert result["model"] == "gemini-test-model"
        assert result["generated_at"]
        assert result["portfolio"]["id"] == my_funded_portfolio.pk
        assert Decimal(result["portfolio"]["market_value"]) == Decimal("200000")
        assert result["excluded"] == []
        # Called once, with the prompt this service built.
        assert generate.call_count == 1
        sent = generate.call_args.args[0]
        assert "OBSERVATIONS ONLY" in sent and "RELIANCE.NS" in sent

    def test_excluded_tickers_travel_with_the_answer(self, with_dead_ticker, fake_gemini):
        with fake_gemini():
            result = build_ai_insights(with_dead_ticker.pk)

        assert [e["ticker"] for e in result["excluded"]] == ["DEADCO.NS"]

    @pytest.mark.parametrize(
        "error, reason",
        [
            (gemini.GeminiRateLimited("429"), "rate_limited"),
            (gemini.GeminiUnavailable("timeout"), "unavailable"),
            (gemini.GeminiNotConfigured("no key"), "not_configured"),
            (gemini.GeminiBadResponse("blocked"), "bad_response"),
        ],
    )
    def test_gemini_failures_are_a_calm_response_not_an_exception(
        self, my_funded_portfolio, fake_gemini, error, reason
    ):
        with fake_gemini(side_effect=error):
            result = build_ai_insights(my_funded_portfolio.pk)

        assert result["available"] is False
        assert result["insights"] == []
        assert result["reason"] == reason
        assert result["message"] == UNAVAILABLE_MESSAGE
        assert result["disclaimer"] == DISCLAIMER
        # The portfolio block is still there, so the panel can name what it
        # could not describe.
        assert result["portfolio"]["name"]

    def test_an_answer_that_is_not_json_is_unavailable_not_a_crash(
        self, my_funded_portfolio, fake_gemini
    ):
        with fake_gemini(return_value="Sure! Here are your insights:\n1. ..."):
            result = build_ai_insights(my_funded_portfolio.pk)

        assert result["available"] is False
        assert result["reason"] == "bad_response"

    def test_all_advice_yields_an_empty_but_available_list(self, my_funded_portfolio, fake_gemini):
        answer = gemini_json([{"category": "risk", "text": "You should sell everything."}])
        with fake_gemini(return_value=answer):
            result = build_ai_insights(my_funded_portfolio.pk)

        assert result["available"] is True
        assert result["insights"] == []
        assert result["disclaimer"] == DISCLAIMER

    def test_data_errors_still_propagate(self, app_user, fake_gemini):
        """An empty portfolio is the portfolio's problem, not the model's."""
        from common.exceptions import EmptyPortfolioError

        portfolio = app_user.user.portfolios.first()
        with fake_gemini() as generate, pytest.raises(EmptyPortfolioError):
            build_ai_insights(portfolio.pk)
        # And the model was never asked - nothing to spend a quota on.
        assert generate.call_count == 0

    def test_not_configured_is_reported_without_a_network_call(self, my_funded_portfolio, gemini_settings):
        """
        The real `generate` with no key: it must raise GeminiNotConfigured
        BEFORE building a request, and the service must render that calmly.
        No patch here on purpose - this exercises the guard itself.
        """
        gemini_settings.GEMINI_API_KEY = ""

        result = build_ai_insights(my_funded_portfolio.pk)

        assert result["available"] is False
        assert result["reason"] == "not_configured"
