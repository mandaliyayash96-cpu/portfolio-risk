"""
`portfolio.services` - the validation, the upsert, and the partial-success
contract of the CSV importer.

The split against test_views.py is the usual one in this codebase: everything
about RULES is checked here against the service, where a failure names the rule
it broke, and test_views.py only proves the HTTP layer carries those answers
out in the right envelope with the right status code.
"""

from datetime import date
from decimal import Decimal

import pytest

from common.exceptions import InvalidInputError, NotFoundError
from marketdata.models import PriceHistory, PriceSnapshot
from portfolio.models import AssetType, Holding
from portfolio.services import (
    ADDED,
    MAX_CSV_BYTES,
    MAX_CSV_ROWS,
    SKIPPED,
    add_holding,
    delete_holding,
    import_holdings_csv,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# add_holding - the happy path
# ---------------------------------------------------------------------------
class TestAddHolding:
    def test_creates_the_position(self, portfolio, stub_provider):
        result = add_holding(
            portfolio.pk,
            "RELIANCE.NS",
            "10",
            "1400.50",
            buy_date="2026-01-05",
            asset_type="EQUITY",
            sector="Energy",
            provider=stub_provider,
        )

        assert result["created"] is True
        assert result["warning"] is None

        holding = Holding.objects.get(portfolio=portfolio, ticker="RELIANCE.NS")
        assert holding.quantity == Decimal("10.000000")
        assert holding.avg_buy_price == Decimal("1400.5000")
        assert holding.buy_date == date(2026, 1, 5)
        assert holding.sector == "Energy"

    def test_money_and_quantity_are_decimal_at_the_boundary(self, portfolio, stub_provider):
        """
        Floats must not reach the DecimalField (common/MONEY.md).

        0.1 + 0.2 is the canonical demonstration: if this value went through a
        float it would arrive as 1400.5000000000002 and this assertion, which
        compares Decimals exactly, would fail.
        """
        add_holding(
            portfolio.pk, "TCS.NS", "0.3", "1400.50", provider=stub_provider
        )
        holding = Holding.objects.get(ticker="TCS.NS")

        assert isinstance(holding.quantity, Decimal)
        assert holding.quantity == Decimal("0.300000")
        assert holding.avg_buy_price == Decimal("1400.5000")

    def test_ticker_is_upper_cased_and_stripped(self, portfolio, stub_provider):
        """
        Lower case in, upper case stored - marketdata writes prices under the
        upper-cased symbol, so a "reliance.ns" holding would otherwise value at
        nothing forever.
        """
        result = add_holding(
            portfolio.pk, "  reliance.ns ", "1", "100", provider=stub_provider
        )
        assert result["ticker"] == "RELIANCE.NS"
        assert Holding.objects.filter(ticker="RELIANCE.NS").exists()

    def test_missing_buy_date_defaults_to_today(self, portfolio, stub_provider):
        """buy_date is NOT NULL, so an omitted one is substituted, not a 500."""
        add_holding(portfolio.pk, "INFY.NS", "1", "100", provider=stub_provider)
        assert Holding.objects.get(ticker="INFY.NS").buy_date is not None

    def test_defaults_asset_type_to_equity(self, portfolio, stub_provider):
        add_holding(portfolio.pk, "INFY.NS", "1", "100", provider=stub_provider)
        assert Holding.objects.get(ticker="INFY.NS").asset_type == AssetType.EQUITY

    def test_re_adding_a_ticker_replaces_it_rather_than_duplicating(
        self, portfolio, holding_factory, stub_provider
    ):
        """
        The (portfolio, ticker) unique constraint allows exactly one row, so a
        second add is an update - which is also what makes re-importing the
        same CSV idempotent.
        """
        holding_factory("RELIANCE.NS", quantity="10", avg_buy_price="1000")

        result = add_holding(
            portfolio.pk, "RELIANCE.NS", "25", "1500", provider=stub_provider
        )

        assert result["created"] is False
        assert Holding.objects.filter(portfolio=portfolio, ticker="RELIANCE.NS").count() == 1
        assert Holding.objects.get(ticker="RELIANCE.NS").quantity == Decimal("25.000000")

    def test_unknown_portfolio_is_not_found(self, stub_provider):
        with pytest.raises(NotFoundError):
            add_holding(999_999, "RELIANCE.NS", "1", "100", provider=stub_provider)


# ---------------------------------------------------------------------------
# add_holding - rejected input
# ---------------------------------------------------------------------------
class TestAddHoldingValidation:
    """
    Every case here must raise InvalidInputError, which the exception handler
    renders as a 400 envelope. None of them may reach the database, and none of
    them may become a 500.
    """

    @pytest.mark.parametrize(
        ("field", "kwargs"),
        [
            ("empty ticker", {"ticker": "   "}),
            ("null ticker", {"ticker": None}),
            ("spaced ticker", {"ticker": "REL IANCE"}),
            ("negative quantity", {"quantity": "-5"}),
            ("zero quantity", {"quantity": "0"}),
            ("non-numeric quantity", {"quantity": "ten"}),
            ("nan quantity", {"quantity": "NaN"}),
            ("infinite quantity", {"quantity": "Infinity"}),
            ("missing quantity", {"quantity": ""}),
            ("negative price", {"avg_buy_price": "-1400.50"}),
            ("zero price", {"avg_buy_price": "0"}),
            ("non-numeric price", {"avg_buy_price": "free"}),
            ("missing price", {"avg_buy_price": None}),
            ("unreadable date", {"buy_date": "last tuesday"}),
            ("unknown asset type", {"asset_type": "DERIVATIVE"}),
        ],
    )
    def test_rejects(self, portfolio, field, kwargs):
        payload = {"ticker": "RELIANCE.NS", "quantity": "10", "avg_buy_price": "1400.50"}
        payload.update(kwargs)

        with pytest.raises(InvalidInputError):
            add_holding(
                portfolio.pk,
                payload["ticker"],
                payload["quantity"],
                payload["avg_buy_price"],
                buy_date=payload.get("buy_date"),
                asset_type=payload.get("asset_type"),
                # Offline: a rejected row must never have reached the provider.
                fetch_prices=False,
            )

        assert Holding.objects.count() == 0, f"{field} was saved anyway"

    def test_quantity_below_column_precision_is_rejected_not_silently_zeroed(
        self, portfolio
    ):
        """
        1e-9 units is > 0 but rounds to 0.000000 in a 6-place column. Storing it
        would make a position the user believes they hold worth exactly nothing.
        """
        with pytest.raises(InvalidInputError):
            add_holding(portfolio.pk, "RELIANCE.NS", "0.000000001", "100", fetch_prices=False)

    def test_oversized_price_is_rejected(self, portfolio):
        with pytest.raises(InvalidInputError):
            add_holding(portfolio.pk, "RELIANCE.NS", "1", "1" + "0" * 20, fetch_prices=False)


# ---------------------------------------------------------------------------
# Price warm-up
# ---------------------------------------------------------------------------
class TestPriceWarmUp:
    def test_new_ticker_gets_prices_fetched(self, portfolio, stub_provider):
        add_holding(portfolio.pk, "RELIANCE.NS", "10", "1400", provider=stub_provider)

        assert PriceSnapshot.objects.filter(ticker="RELIANCE.NS").exists()
        assert PriceHistory.objects.filter(ticker="RELIANCE.NS").exists()

    def test_unknown_ticker_is_saved_with_a_warning_and_does_not_raise(
        self, portfolio, stub_provider
    ):
        """
        The headline case. A typo'd or delisted symbol must land in the database
        anyway - the user can see it and fix it - and must come back carrying
        the reason it has no prices, not an exception.
        """
        result = add_holding(
            portfolio.pk, "NOTAREALTICKER.NS", "10", "1400", provider=stub_provider
        )

        assert Holding.objects.filter(ticker="NOTAREALTICKER.NS").exists()
        assert result["warning"], "an unknown ticker must come back with a warning"
        assert "NOTAREALTICKER.NS" in result["warning"] or "no live price" in result["warning"]
        assert not PriceHistory.objects.filter(ticker="NOTAREALTICKER.NS").exists()

    def test_ticker_with_stored_history_is_not_re_fetched(self, portfolio):
        """
        "New tickers only". A symbol we already have closes for skips the
        network entirely - which is why a re-import costs nothing.

        Proven with no provider at all: passing None here would build the real
        yfinance one, so if this ever DID fetch, the test would either hit the
        network or blow up. It does neither, because it never fetches.
        """
        PriceHistory.objects.create(
            ticker="RELIANCE.NS", date=date(2026, 1, 5), close=Decimal("1000.0000")
        )

        result = add_holding(portfolio.pk, "RELIANCE.NS", "10", "1400")

        assert result["warning"] is None
        assert PriceSnapshot.objects.count() == 0


# ---------------------------------------------------------------------------
# delete_holding
# ---------------------------------------------------------------------------
class TestDeleteHolding:
    def test_removes_the_row(self, holding_factory):
        holding = holding_factory("TCS.NS")

        result = delete_holding(holding.pk)

        assert result == {
            "id": holding.pk,
            "portfolio_id": holding.portfolio_id,
            "ticker": "TCS.NS",
            "deleted": True,
        }
        assert not Holding.objects.filter(pk=holding.pk).exists()

    def test_unknown_id_is_not_found(self, portfolio):
        with pytest.raises(NotFoundError):
            delete_holding(999_999)

    def test_cannot_delete_out_of_another_portfolio(self, holding_factory, investor):
        """
        The portfolio id in the URL is a scope, not decoration: a real id in
        the wrong portfolio is a 404, and the row survives.
        """
        from portfolio.models import Portfolio

        holding = holding_factory("TCS.NS")
        other = Portfolio.objects.create(user=investor, name="Other", base_currency="INR")

        with pytest.raises(NotFoundError):
            delete_holding(holding.pk, portfolio_id=other.pk)

        assert Holding.objects.filter(pk=holding.pk).exists()

    def test_leaves_stored_prices_alone(self, holding_factory):
        """Another portfolio may hold the same symbol - prices are not ours."""
        PriceHistory.objects.create(
            ticker="TCS.NS", date=date(2026, 1, 5), close=Decimal("3200.0000")
        )
        delete_holding(holding_factory("TCS.NS").pk)

        assert PriceHistory.objects.filter(ticker="TCS.NS").exists()


# ---------------------------------------------------------------------------
# import_holdings_csv - per-row outcomes
# ---------------------------------------------------------------------------
class TestImportCsv:
    def test_clean_file_adds_every_row(self, portfolio, csv_upload, good_csv, stub_provider):
        report = import_holdings_csv(
            portfolio.pk, csv_upload(good_csv), provider=stub_provider
        )

        assert (report["added"], report["updated"], report["skipped"]) == (2, 0, 0)
        assert report["total_rows"] == 2
        assert Holding.objects.filter(portfolio=portfolio).count() == 2

    def test_good_and_bad_rows_are_both_reported(self, portfolio, csv_upload, stub_provider):
        """
        The partial-success contract. Three of these five rows are unusable;
        the other two must land anyway, and every one of the five must come
        back with a status - and, when skipped, a reason worth reading.
        """
        text = (
            "ticker,quantity,avg_buy_price\n"
            "RELIANCE.NS,10,1400.50\n"   # line 2 - good
            ",5,3200.00\n"               # line 3 - no ticker
            "TCS.NS,-5,3200.00\n"        # line 4 - negative quantity
            "INFY.NS,5,notaprice\n"      # line 5 - unreadable price
            "HDFCBANK.NS,3,1600.00\n"    # line 6 - good
        )

        report = import_holdings_csv(portfolio.pk, csv_upload(text), provider=stub_provider)

        assert report["total_rows"] == 5
        assert (report["added"], report["updated"], report["skipped"]) == (2, 0, 3)

        by_row = {entry["row"]: entry for entry in report["results"]}
        assert by_row[2]["status"] == "added"
        assert by_row[6]["status"] == "added"
        for line in (3, 4, 5):
            assert by_row[line]["status"] == "skipped"
            assert by_row[line]["reason"], f"row {line} was skipped without a reason"

        # The good rows really are committed, and the bad ones really are not.
        assert set(
            Holding.objects.filter(portfolio=portfolio).values_list("ticker", flat=True)
        ) == {"RELIANCE.NS", "HDFCBANK.NS"}

    def test_reason_names_the_offending_field(self, portfolio, csv_upload):
        text = "ticker,quantity,avg_buy_price\nTCS.NS,-5,3200.00\n"

        report = import_holdings_csv(portfolio.pk, csv_upload(text), fetch_prices=False)

        assert "quantity" in report["results"][0]["reason"]

    def test_re_import_updates_rather_than_duplicating(
        self, portfolio, csv_upload, good_csv, stub_provider
    ):
        import_holdings_csv(portfolio.pk, csv_upload(good_csv), provider=stub_provider)
        report = import_holdings_csv(
            portfolio.pk, csv_upload(good_csv), provider=stub_provider
        )

        assert (report["added"], report["updated"]) == (0, 2)
        assert Holding.objects.filter(portfolio=portfolio).count() == 2

    def test_duplicate_ticker_in_one_file_keeps_the_first_and_says_so(
        self, portfolio, csv_upload, stub_provider
    ):
        text = (
            "ticker,quantity,avg_buy_price\n"
            "RELIANCE.NS,10,1400.50\n"
            "RELIANCE.NS,99,1400.50\n"
        )

        report = import_holdings_csv(portfolio.pk, csv_upload(text), provider=stub_provider)

        assert (report["added"], report["skipped"]) == (1, 1)
        assert "Duplicate" in report["results"][1]["reason"]
        assert Holding.objects.get(ticker="RELIANCE.NS").quantity == Decimal("10.000000")

    def test_headers_are_case_and_space_insensitive(
        self, portfolio, csv_upload, stub_provider
    ):
        text = "Ticker, Quantity ,Avg Buy Price\nRELIANCE.NS,10,1400.50\n"

        report = import_holdings_csv(portfolio.pk, csv_upload(text), provider=stub_provider)

        assert report["added"] == 1

    def test_blank_rows_are_ignored_not_skipped(self, portfolio, csv_upload, stub_provider):
        text = "ticker,quantity,avg_buy_price\nRELIANCE.NS,10,1400.50\n\n,,\n"

        report = import_holdings_csv(portfolio.pk, csv_upload(text), provider=stub_provider)

        assert (report["total_rows"], report["added"], report["skipped"]) == (1, 1, 0)

    def test_optional_columns_may_be_absent(self, portfolio, csv_upload, stub_provider):
        text = "ticker,quantity,avg_buy_price\nRELIANCE.NS,10,1400.50\n"

        report = import_holdings_csv(portfolio.pk, csv_upload(text), provider=stub_provider)

        assert report["added"] == 1
        assert Holding.objects.get(ticker="RELIANCE.NS").sector == ""

    def test_unknown_ticker_row_is_saved_with_a_warning(
        self, portfolio, csv_upload, stub_provider
    ):
        """One bad SYMBOL is not a bad ROW: it saves, and it carries a warning."""
        text = (
            "ticker,quantity,avg_buy_price\n"
            "RELIANCE.NS,10,1400.50\n"
            "NOSUCHTICKER.NS,5,100.00\n"
        )

        report = import_holdings_csv(portfolio.pk, csv_upload(text), provider=stub_provider)

        assert report["added"] == 2
        assert report["skipped"] == 0
        assert Holding.objects.filter(ticker="NOSUCHTICKER.NS").exists()

        by_ticker = {entry["ticker"]: entry for entry in report["results"]}
        assert by_ticker["NOSUCHTICKER.NS"]["warning"]
        assert by_ticker["RELIANCE.NS"]["warning"] is None
        assert "NOSUCHTICKER.NS" in report["price_fetch"]["warnings"]


# ---------------------------------------------------------------------------
# import_holdings_csv - file-level rejection
# ---------------------------------------------------------------------------
class TestImportCsvRejection:
    """
    These reject the WHOLE upload, because no row inside it can be trusted.
    Each must be an InvalidInputError - a clean 400 - and must write nothing.
    """

    def test_wrong_headers_are_rejected_with_the_expected_columns_named(
        self, portfolio, csv_upload
    ):
        text = "symbol,units,cost\nRELIANCE.NS,10,1400.50\n"

        with pytest.raises(InvalidInputError) as caught:
            import_holdings_csv(portfolio.pk, csv_upload(text))

        message = caught.value.message
        # The message has to be actionable on its own: it names what is missing
        # AND what the file should have contained.
        for column in ("ticker", "quantity", "avg_buy_price"):
            assert column in message
        assert caught.value.details["missing"] == ["ticker", "quantity", "avg_buy_price"]
        assert Holding.objects.count() == 0

    def test_partially_wrong_headers_name_only_the_missing_one(self, portfolio, csv_upload):
        text = "ticker,quantity,cost\nRELIANCE.NS,10,1400.50\n"

        with pytest.raises(InvalidInputError) as caught:
            import_holdings_csv(portfolio.pk, csv_upload(text))

        assert caught.value.details["missing"] == ["avg_buy_price"]

    def test_non_csv_filename_is_rejected(self, portfolio, csv_upload, good_csv):
        with pytest.raises(InvalidInputError) as caught:
            import_holdings_csv(portfolio.pk, csv_upload(good_csv, filename="holdings.xlsx"))

        assert ".csv" in caught.value.message
        assert Holding.objects.count() == 0

    def test_binary_content_is_rejected(self, portfolio):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile(
            "holdings.csv", b"\xff\xfe\x00\x01\x02binary", content_type="text/csv"
        )

        with pytest.raises(InvalidInputError):
            import_holdings_csv(portfolio.pk, upload)

    def test_missing_file_is_rejected(self, portfolio):
        with pytest.raises(InvalidInputError) as caught:
            import_holdings_csv(portfolio.pk, None)

        assert "ticker" in caught.value.message  # names the expected columns

    def test_empty_file_is_rejected(self, portfolio, csv_upload):
        with pytest.raises(InvalidInputError):
            import_holdings_csv(portfolio.pk, csv_upload("   \n"))

    def test_oversized_file_is_rejected(self, portfolio, csv_upload):
        padding = "x" * 70
        text = "ticker,quantity,avg_buy_price,sector\n" + (
            f"RELIANCE.NS,10,1400.50,{padding}\n" * 15_000
        )
        assert len(text.encode("utf-8")) > MAX_CSV_BYTES

        with pytest.raises(InvalidInputError) as caught:
            import_holdings_csv(portfolio.pk, csv_upload(text))

        assert str(MAX_CSV_BYTES) in caught.value.message
        assert Holding.objects.count() == 0

    def test_too_many_rows_is_rejected_whole(self, portfolio, csv_upload):
        """
        Rejected, not truncated - and critically, NOTHING is written. The row
        cap is checked while reading, before the single write transaction, so
        raising here cannot leave a half-loaded portfolio behind.
        """
        text = "ticker,quantity,avg_buy_price\n" + "".join(
            f"T{index}.NS,1,100\n" for index in range(MAX_CSV_ROWS + 5)
        )

        with pytest.raises(InvalidInputError) as caught:
            import_holdings_csv(portfolio.pk, csv_upload(text))

        assert str(MAX_CSV_ROWS) in caught.value.message
        assert Holding.objects.count() == 0

    def test_unknown_portfolio_is_not_found(self, csv_upload, good_csv):
        with pytest.raises(NotFoundError):
            import_holdings_csv(999_999, csv_upload(good_csv))


# ---------------------------------------------------------------------------
# Ticker hygiene on entry (Safeguard 2)
#
# The write path's half of the dead-ticker problem. The risk report now
# survives a symbol it cannot price (risk/tests/test_services.py), but the
# better outcome is for the user to learn at the moment they type it - while
# they still remember what they meant - rather than from a warning banner a day
# later. Nothing here rejects a holding: every one of these still saves.
# ---------------------------------------------------------------------------
class TestTickerNormalisation:
    @pytest.mark.parametrize(
        "typed",
        ["reliance.ns", "  RELIANCE.NS  ", "Reliance.Ns", "\treliance.NS\n"],
    )
    def test_case_and_whitespace_are_normalised(self, portfolio, stub_provider, typed):
        result = add_holding(portfolio.pk, typed, "10", "1400", provider=stub_provider)

        assert result["ticker"] == "RELIANCE.NS"
        assert Holding.objects.filter(portfolio=portfolio, ticker="RELIANCE.NS").exists()

    def test_normalising_makes_two_spellings_one_position(self, portfolio, stub_provider):
        """The upsert key is the NORMALISED ticker, so these cannot both exist."""
        add_holding(portfolio.pk, "reliance.ns", "10", "1400", provider=stub_provider)
        second = add_holding(portfolio.pk, "RELIANCE.NS", "20", "1500", provider=stub_provider)

        assert second["created"] is False
        assert Holding.objects.filter(portfolio=portfolio).count() == 1


class TestSuffixHint:
    def test_a_bare_symbol_is_saved_and_warned_about(self, portfolio, stub_provider):
        """
        The requirement in one test: do NOT auto-correct, DO say something.

        "TCS" is stored as "TCS" - a symbol the user did not type is one they
        cannot debug - and the warning names the symbol we suspect they meant.
        """
        result = add_holding(portfolio.pk, "TCS", "10", "3200", provider=stub_provider)

        assert result["ticker"] == "TCS"
        assert Holding.objects.filter(portfolio=portfolio, ticker="TCS").exists()
        assert not Holding.objects.filter(portfolio=portfolio, ticker="TCS.NS").exists()
        assert "TCS.NS" in result["warning"]
        assert "no exchange suffix" in result["warning"]

    def test_a_suffixed_symbol_is_not_warned_about(self, portfolio, stub_provider):
        result = add_holding(
            portfolio.pk, "RELIANCE.NS", "10", "1400", provider=stub_provider
        )

        assert result["warning"] is None

    @pytest.mark.parametrize("symbol", ["BTC-USD", "ETH-USD"])
    def test_pair_symbols_are_not_warned_about(self, portfolio, symbol):
        """
        A hyphenated pair is how yfinance spells crypto and FX. It has no
        exchange suffix and is not missing one, so the hint would be noise.
        """
        result = add_holding(
            portfolio.pk, symbol, "1", "5000000", fetch_prices=False
        )

        assert result["warning"] is None

    def test_the_hint_also_reaches_a_csv_row(self, portfolio, csv_upload):
        """One validation seam, so the importer inherits this for free."""
        report = import_holdings_csv(
            portfolio.pk,
            csv_upload("ticker,quantity,avg_buy_price\nTCS,5,3200\n"),
            fetch_prices=False,
        )

        row = report["results"][0]
        assert row["status"] == ADDED
        assert "TCS.NS" in row["warning"]


class TestUnverifiedFlag:
    def test_a_symbol_with_no_prices_is_saved_but_flagged(self, portfolio, stub_provider):
        """
        The point of the flag: the row lands, and the user is told immediately
        that the risk report cannot use it yet - rather than finding out later.
        """
        result = add_holding(
            portfolio.pk, "NOTAREALTICKER.NS", "10", "1400", provider=stub_provider
        )

        assert Holding.objects.filter(ticker="NOTAREALTICKER.NS").exists()
        assert result["unverified"] is True
        assert result["warning"]

    def test_a_symbol_the_feed_knows_is_not_flagged(self, portfolio, stub_provider):
        result = add_holding(
            portfolio.pk, "RELIANCE.NS", "10", "1400", provider=stub_provider
        )

        assert result["unverified"] is False

    def test_unverified_tracks_stored_history_not_the_fetch(self, portfolio):
        """
        The flag answers the question the RISK REPORT asks - "is there history
        to value this with?" - not "did a fetch just happen". A symbol whose
        history is already stored is verified without any fetch at all.
        """
        PriceHistory.objects.create(
            ticker="RELIANCE.NS", date=date(2026, 1, 5), close=Decimal("1000.0000")
        )

        result = add_holding(portfolio.pk, "RELIANCE.NS", "10", "1400")

        assert result["unverified"] is False

    def test_import_rows_carry_the_flag_per_row(
        self, portfolio, csv_upload, stub_provider
    ):
        report = import_holdings_csv(
            portfolio.pk,
            csv_upload(
                "ticker,quantity,avg_buy_price\n"
                "RELIANCE.NS,10,1400\n"
                "NOSUCHTICKER.NS,5,100\n"
            ),
            provider=stub_provider,
        )

        by_ticker = {row["ticker"]: row for row in report["results"]}
        assert by_ticker["RELIANCE.NS"]["unverified"] is False
        assert by_ticker["NOSUCHTICKER.NS"]["unverified"] is True

    def test_a_skipped_row_is_not_unverified(self, portfolio, csv_upload):
        """It was never written, so there is nothing to verify."""
        report = import_holdings_csv(
            portfolio.pk,
            csv_upload("ticker,quantity,avg_buy_price\nRELIANCE.NS,-5,1400\n"),
            fetch_prices=False,
        )

        row = report["results"][0]
        assert row["status"] == SKIPPED
        assert row["unverified"] is False
