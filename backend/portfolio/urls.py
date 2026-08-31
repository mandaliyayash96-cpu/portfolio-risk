"""
Holdings endpoints, mounted at /api/portfolio/ by config/urls.py.

Everything here is addressed by PORTFOLIO first, including the single-holding
delete. A holding is only meaningful inside the portfolio that owns it, and
spelling the owner in the URL is what lets the view scope its lookup rather
than trust a bare id - see portfolio.selectors.get_holding.

`import/` is listed before the `<int:holding_id>/` route for readability only:
the int converter cannot match "import", so the two could not collide.
"""

from django.urls import path

from portfolio import views

app_name = "portfolio"

urlpatterns = [
    path("<int:portfolio_id>/holdings/", views.holdings, name="holdings"),
    path("<int:portfolio_id>/holdings/import/", views.holdings_import, name="holdings-import"),
    path(
        "<int:portfolio_id>/holdings/<int:holding_id>/",
        views.holding_detail,
        name="holding-detail",
    ),
]
