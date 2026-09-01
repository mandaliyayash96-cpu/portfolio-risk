"""
Payment endpoints, mounted at /api/payments/ by config/urls.py.

Three POSTs and nothing else. There is no GET listing a user's payments: the
dashboard has no screen for it, and an endpoint that exposes a payment history
nobody renders is a surface with no purpose. The admin has the same data.

There is also no "am I unlocked?" endpoint, on purpose. The dashboard must lock
on every reload - that is what makes each visit a paid round - so a client that
could ask "am I still unlocked" would be a client that could skip paying.
"""

from django.urls import path

from payments import views

app_name = "payments"

urlpatterns = [
    path("order/", views.create_order, name="order"),
    path("verify/", views.verify, name="verify"),
    path("finish/", views.finish, name="finish"),
]
