"""
The account service layer, with no HTTP in the way.

`resolve_app_user` runs on EVERY authenticated request, so the properties that
matter most are idempotence and the absence of writes on the returning-user
path - both of which are cheaper to state here than through a view.
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import AppUser
from accounts.selectors import find_my_portfolio, get_my_portfolio, is_app_user
from accounts.services import (
    DEFAULT_PORTFOLIO_NAME,
    ensure_portfolio,
    normalise_phone,
    resolve_app_user,
    start_session,
)
from accounts.tests.conftest import OTHER_PHONE, PHONE
from common.exceptions import InvalidInputError, NotFoundError
from portfolio.models import Portfolio

pytestmark = pytest.mark.django_db


class TestResolveAppUser:
    def test_first_call_creates_user_and_portfolio(self):
        app_user = resolve_app_user(PHONE, firebase_uid="uid-1")

        assert app_user.phone_number == PHONE
        assert AppUser.objects.count() == 1
        assert Portfolio.objects.filter(user=app_user.user).count() == 1
        assert Portfolio.objects.get(user=app_user.user).name == DEFAULT_PORTFOLIO_NAME

    def test_second_call_returns_the_same_rows(self):
        first = resolve_app_user(PHONE)
        second = resolve_app_user(PHONE)

        assert first.pk == second.pk
        assert AppUser.objects.count() == 1
        assert Portfolio.objects.count() == 1

    def test_the_returning_path_writes_nothing(self):
        """
        This runs on every authenticated request. An UPDATE here would be an
        UPDATE on every request - `start_session` is where the login stamp and
        the uid refresh belong.
        """
        resolve_app_user(PHONE, firebase_uid="uid-1")

        with CaptureQueriesContext(connection) as queries:
            resolve_app_user(PHONE, firebase_uid="uid-2")

        statements = [q["sql"].strip().upper() for q in queries]
        assert all(sql.startswith("SELECT") for sql in statements), statements

    def test_distinct_numbers_get_distinct_portfolios(self):
        first = resolve_app_user(PHONE)
        second = resolve_app_user(OTHER_PHONE)

        assert first.user_id != second.user_id
        assert Portfolio.objects.count() == 2

    @pytest.mark.parametrize("bad", [None, "", "   ", "+" + "9" * 40])
    def test_an_unusable_phone_claim_is_rejected(self, bad):
        with pytest.raises(InvalidInputError):
            resolve_app_user(bad)

        assert AppUser.objects.count() == 0

    def test_the_phone_number_is_stored_verbatim(self):
        """
        E.164 out of the token, E.164 in the column. Reformatting it here would
        mean the same person keying to two identities the day the rule changes.
        """
        app_user = resolve_app_user("  " + PHONE + "  ")

        assert app_user.phone_number == PHONE

    def test_a_username_collision_does_not_block_sign_in(self):
        """A hand-made admin account happening to be named like a phone number."""
        get_user_model().objects.create_user(username=PHONE, password="x")

        app_user = resolve_app_user(PHONE)

        assert app_user.user.username != PHONE
        assert Portfolio.objects.filter(user=app_user.user).count() == 1


class TestNormalisePhone:
    def test_trims_but_does_not_reformat(self):
        assert normalise_phone(" +919876543210 ") == "+919876543210"

    def test_rejects_empty(self):
        with pytest.raises(InvalidInputError):
            normalise_phone("   ")


class TestEnsurePortfolio:
    def test_is_idempotent(self):
        app_user = resolve_app_user(PHONE)

        first = ensure_portfolio(app_user)
        second = ensure_portfolio(app_user)

        assert first.pk == second.pk
        assert Portfolio.objects.count() == 1

    def test_recreates_a_deleted_portfolio(self):
        app_user = resolve_app_user(PHONE)
        Portfolio.objects.all().delete()

        portfolio = ensure_portfolio(app_user)

        assert portfolio.pk is not None
        assert Portfolio.objects.count() == 1

    def test_keeps_a_renamed_portfolio_instead_of_adding_one(self):
        app_user = resolve_app_user(PHONE)
        portfolio = Portfolio.objects.get(user=app_user.user)
        portfolio.name = "Retirement"
        portfolio.save(update_fields=["name"])

        assert ensure_portfolio(app_user).pk == portfolio.pk
        assert Portfolio.objects.count() == 1


class TestStartSession:
    def test_stamps_the_login_and_reports_the_first_one(self):
        app_user = resolve_app_user(PHONE)

        _, _, first_login = start_session(app_user)

        assert first_login is True
        assert AppUser.objects.get(pk=app_user.pk).last_login_at is not None

    def test_the_second_session_is_not_a_first_login(self):
        app_user = resolve_app_user(PHONE)
        start_session(app_user)

        _, _, first_login = start_session(app_user)

        assert first_login is False

    def test_refreshes_the_stored_uid(self):
        app_user = resolve_app_user(PHONE, firebase_uid="uid-before")

        start_session(app_user, firebase_uid="uid-after")

        assert AppUser.objects.get(pk=app_user.pk).firebase_uid == "uid-after"


class TestSelectors:
    def test_get_my_portfolio_returns_the_owned_one(self):
        app_user = resolve_app_user(PHONE)
        other = resolve_app_user(OTHER_PHONE)

        mine = get_my_portfolio(app_user.user)

        assert mine.user_id == app_user.user_id
        assert mine.pk != get_my_portfolio(other.user).pk

    def test_get_my_portfolio_404s_when_there_is_none(self):
        app_user = resolve_app_user(PHONE)
        Portfolio.objects.all().delete()

        with pytest.raises(NotFoundError):
            get_my_portfolio(app_user.user)

    def test_find_my_portfolio_is_none_for_anonymous(self):
        from django.contrib.auth.models import AnonymousUser

        assert find_my_portfolio(AnonymousUser()) is None
        assert find_my_portfolio(None) is None

    def test_is_app_user_separates_investors_from_other_logins(self):
        app_user = resolve_app_user(PHONE)
        admin = get_user_model().objects.create_superuser(
            username="root3", password="pw", email=""
        )

        assert is_app_user(app_user.user) is True
        assert is_app_user(admin) is False
