"""
The three holdings endpoints.

test_services.py owns the rules; this file owns the WIRE. What it proves is
narrow on purpose: the right status code, the {success, data, error} envelope
on both halves, and - the one that matters most - that a bad upload is a 400
carrying a message, never a 500 carrying a traceback.

TWO CLIENTS, AND WHICH IS WHICH
-------------------------------
`api` is anonymous, and the READ tests use it: viewing a portfolio is free and
needs no identity. `editor` is signed in AND holds a paid ₹9 unlock, and every
WRITE test uses it, because a write now needs both - 401 without an account,
402 without a live grant.

Neither of those refusals is tested here; `payments/tests/test_gate.py` owns
them, and owns the question of when a grant is spent. This file assumes a paid
round is open and asks what the endpoints do inside one.
"""

from decimal import Decimal

import pytest

from portfolio.models import Holding

pytestmark = pytest.mark.django_db


def url(portfolio_id: int) -> str:
    return f"/api/portfolio/{portfolio_id}/holdings/"


def import_url(portfolio_id: int) -> str:
    return f"/api/portfolio/{portfolio_id}/holdings/import/"


class TestAddEndpoint:
    def test_valid_add_returns_201_in_the_success_envelope(
        self, editor, portfolio, stub_provider_setting
    ):
        response = editor.post(
            url(portfolio.pk),
            {"ticker": "RELIANCE.NS", "quantity": "10", "avg_buy_price": "1400.50"},
            format="json",
        )

        assert response.status_code == 201
        body = response.json()
        assert body["success"] is True
        assert body["error"] is None
        assert body["data"]["ticker"] == "RELIANCE.NS"
        assert body["data"]["created"] is True
        assert Holding.objects.filter(ticker="RELIANCE.NS").exists()

    def test_replacing_an_existing_ticker_returns_200(
        self, editor, portfolio, holding_factory, stub_provider_setting
    ):
        holding_factory("RELIANCE.NS")

        response = editor.post(
            url(portfolio.pk),
            {"ticker": "RELIANCE.NS", "quantity": "20", "avg_buy_price": "1500"},
            format="json",
        )

        assert response.status_code == 200
        assert response.json()["data"]["created"] is False

    @pytest.mark.parametrize(
        "payload",
        [
            {"ticker": "", "quantity": "10", "avg_buy_price": "1400.50"},
            {"ticker": "RELIANCE.NS", "quantity": "-10", "avg_buy_price": "1400.50"},
            {"ticker": "RELIANCE.NS", "quantity": "10", "avg_buy_price": "-1"},
            {"ticker": "RELIANCE.NS", "quantity": "10", "avg_buy_price": "free"},
            {"quantity": "10", "avg_buy_price": "1400.50"},
            {},
        ],
    )
    def test_bad_input_is_a_400_envelope_not_a_500(self, editor, portfolio, payload):
        response = editor.post(url(portfolio.pk), payload, format="json")

        assert response.status_code == 400
        body = response.json()
        assert body["success"] is False
        assert body["data"] is None
        assert body["error"]["code"] == "invalid_input"
        assert body["error"]["message"]
        assert Holding.objects.count() == 0

    def test_a_bare_json_array_body_does_not_crash(self, editor, portfolio):
        """
        `.get()` on a list raises. The view normalises the shape once, so this
        arrives as an ordinary validation failure rather than a 500.
        """
        response = editor.post(url(portfolio.pk), ["nonsense"], format="json")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"

    def test_the_url_id_is_ignored_for_a_signed_in_caller(
        self, editor, portfolio, stub_provider_setting
    ):
        """
        This used to assert a 404 on an unknown id, and cannot any more.

        Since Part 1 an authenticated write goes to the caller's OWN portfolio,
        so the id in the URL is not consulted: it can neither 404 nor reach
        somebody else's data. The 404-on-a-bad-id contract still holds on the
        anonymous READ path - see TestListEndpoint - and the cross-account
        guarantee is asserted in TestDeleteEndpoint below.
        """
        response = editor.post(
            url(999_999),
            {"ticker": "RELIANCE.NS", "quantity": "1", "avg_buy_price": "100"},
            format="json",
        )

        assert response.status_code == 201
        assert Holding.objects.get(ticker="RELIANCE.NS").portfolio_id == portfolio.pk

    def test_unknown_ticker_still_saves_and_carries_a_warning(
        self, editor, portfolio, stub_provider_setting
    ):
        response = editor.post(
            url(portfolio.pk),
            {"ticker": "NOSUCHTICKER.NS", "quantity": "1", "avg_buy_price": "100"},
            format="json",
        )

        assert response.status_code == 201
        assert response.json()["data"]["warning"]
        assert Holding.objects.filter(ticker="NOSUCHTICKER.NS").exists()


class TestListEndpoint:
    def test_lists_holdings_with_their_ids(self, api, portfolio, holding_factory):
        holding = holding_factory("TCS.NS")

        response = api.get(url(portfolio.pk))

        assert response.status_code == 200
        rows = response.json()["data"]
        assert [row["id"] for row in rows] == [holding.pk]
        # Money stays a string in transit - common/MONEY.md.
        assert isinstance(rows[0]["quantity"], str)

    def test_empty_portfolio_lists_nothing_rather_than_erroring(self, api, portfolio):
        response = api.get(url(portfolio.pk))

        assert response.status_code == 200
        assert response.json()["data"] == []

    def test_unknown_portfolio_is_a_404(self, api):
        assert api.get(url(999_999)).status_code == 404


class TestImportEndpoint:
    def test_good_and_bad_rows_come_back_in_one_report(
        self, editor, portfolio, csv_upload, stub_provider_setting
    ):
        text = (
            "ticker,quantity,avg_buy_price\n"
            "RELIANCE.NS,10,1400.50\n"
            "TCS.NS,-5,3200.00\n"
        )

        response = editor.post(
            import_url(portfolio.pk), {"file": csv_upload(text)}, format="multipart"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert (data["added"], data["skipped"]) == (1, 1)
        assert len(data["results"]) == 2
        assert Holding.objects.count() == 1

    def test_wrong_headers_are_a_400_naming_the_columns(self, editor, portfolio, csv_upload):
        response = editor.post(
            import_url(portfolio.pk),
            {"file": csv_upload("symbol,units,cost\nRELIANCE.NS,10,1400.50\n")},
            format="multipart",
        )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "invalid_input"
        assert "avg_buy_price" in error["message"]
        assert error["details"]["missing"]

    def test_no_file_is_a_400_not_a_500(self, editor, portfolio):
        response = editor.post(import_url(portfolio.pk), {}, format="multipart")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"

    def test_non_csv_upload_is_rejected(self, editor, portfolio, csv_upload, good_csv):
        response = editor.post(
            import_url(portfolio.pk),
            {"file": csv_upload(good_csv, filename="book.xlsx")},
            format="multipart",
        )

        assert response.status_code == 400
        assert Holding.objects.count() == 0


class TestDeleteEndpoint:
    def test_deletes_and_confirms(self, editor, portfolio, holding_factory):
        holding = holding_factory("TCS.NS")

        response = editor.delete(f"{url(portfolio.pk)}{holding.pk}/")

        assert response.status_code == 200
        assert response.json()["data"]["deleted"] is True
        assert not Holding.objects.filter(pk=holding.pk).exists()

    def test_unknown_holding_is_a_404_envelope(self, editor, portfolio):
        response = editor.delete(f"{url(portfolio.pk)}999999/")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_another_investors_holding_cannot_be_deleted(self, editor, portfolio):
        """
        Both ids correct, and it still 404s.

        The delete is scoped to the caller's own portfolio rather than to the
        portfolio named in the URL, so guessing a stranger's holding id buys
        nothing - and a paid unlock does not change that. Paying for a round of
        edits buys edits to YOUR portfolio.
        """
        from datetime import date

        from accounts.selectors import get_my_portfolio
        from accounts.services import resolve_app_user

        stranger = resolve_app_user("+919876500002")
        theirs = Holding.objects.create(
            portfolio=get_my_portfolio(stranger.user),
            ticker="TCS.NS",
            quantity=Decimal("5"),
            avg_buy_price=Decimal("3200.0000"),
            buy_date=date(2026, 1, 6),
        )

        response = editor.delete(f"{url(portfolio.pk)}{theirs.pk}/")

        assert response.status_code == 404
        assert Holding.objects.filter(pk=theirs.pk).exists()
