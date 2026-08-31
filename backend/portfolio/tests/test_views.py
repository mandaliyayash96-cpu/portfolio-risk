"""
The three holdings endpoints.

test_services.py owns the rules; this file owns the WIRE. What it proves is
narrow on purpose: the right status code, the {success, data, error} envelope
on both halves, and - the one that matters most - that a bad upload is a 400
carrying a message, never a 500 carrying a traceback.
"""

import pytest

from portfolio.models import Holding

pytestmark = pytest.mark.django_db


def url(portfolio_id: int) -> str:
    return f"/api/portfolio/{portfolio_id}/holdings/"


def import_url(portfolio_id: int) -> str:
    return f"/api/portfolio/{portfolio_id}/holdings/import/"


class TestAddEndpoint:
    def test_valid_add_returns_201_in_the_success_envelope(
        self, api, portfolio, stub_provider_setting
    ):
        response = api.post(
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
        self, api, portfolio, holding_factory, stub_provider_setting
    ):
        holding_factory("RELIANCE.NS")

        response = api.post(
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
    def test_bad_input_is_a_400_envelope_not_a_500(self, api, portfolio, payload):
        response = api.post(url(portfolio.pk), payload, format="json")

        assert response.status_code == 400
        body = response.json()
        assert body["success"] is False
        assert body["data"] is None
        assert body["error"]["code"] == "invalid_input"
        assert body["error"]["message"]
        assert Holding.objects.count() == 0

    def test_a_bare_json_array_body_does_not_crash(self, api, portfolio):
        """
        `.get()` on a list raises. The view normalises the shape once, so this
        arrives as an ordinary validation failure rather than a 500.
        """
        response = api.post(url(portfolio.pk), ["nonsense"], format="json")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"

    def test_unknown_portfolio_is_a_404_envelope(self, api):
        response = api.post(
            url(999_999),
            {"ticker": "RELIANCE.NS", "quantity": "1", "avg_buy_price": "100"},
            format="json",
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_unknown_ticker_still_saves_and_carries_a_warning(
        self, api, portfolio, stub_provider_setting
    ):
        response = api.post(
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
        self, api, portfolio, csv_upload, stub_provider_setting
    ):
        text = (
            "ticker,quantity,avg_buy_price\n"
            "RELIANCE.NS,10,1400.50\n"
            "TCS.NS,-5,3200.00\n"
        )

        response = api.post(
            import_url(portfolio.pk), {"file": csv_upload(text)}, format="multipart"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert (data["added"], data["skipped"]) == (1, 1)
        assert len(data["results"]) == 2
        assert Holding.objects.count() == 1

    def test_wrong_headers_are_a_400_naming_the_columns(self, api, portfolio, csv_upload):
        response = api.post(
            import_url(portfolio.pk),
            {"file": csv_upload("symbol,units,cost\nRELIANCE.NS,10,1400.50\n")},
            format="multipart",
        )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "invalid_input"
        assert "avg_buy_price" in error["message"]
        assert error["details"]["missing"]

    def test_no_file_is_a_400_not_a_500(self, api, portfolio):
        response = api.post(import_url(portfolio.pk), {}, format="multipart")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"

    def test_non_csv_upload_is_rejected(self, api, portfolio, csv_upload, good_csv):
        response = api.post(
            import_url(portfolio.pk),
            {"file": csv_upload(good_csv, filename="book.xlsx")},
            format="multipart",
        )

        assert response.status_code == 400
        assert Holding.objects.count() == 0


class TestDeleteEndpoint:
    def test_deletes_and_confirms(self, api, portfolio, holding_factory):
        holding = holding_factory("TCS.NS")

        response = api.delete(f"{url(portfolio.pk)}{holding.pk}/")

        assert response.status_code == 200
        assert response.json()["data"]["deleted"] is True
        assert not Holding.objects.filter(pk=holding.pk).exists()

    def test_unknown_holding_is_a_404_envelope(self, api, portfolio):
        response = api.delete(f"{url(portfolio.pk)}999999/")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_wrong_portfolio_cannot_delete(self, api, holding_factory, investor):
        from portfolio.models import Portfolio

        holding = holding_factory("TCS.NS")
        other = Portfolio.objects.create(user=investor, name="Other", base_currency="INR")

        response = api.delete(f"{url(other.pk)}{holding.pk}/")

        assert response.status_code == 404
        assert Holding.objects.filter(pk=holding.pk).exists()
