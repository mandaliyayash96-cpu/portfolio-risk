"""
Auth endpoints, mounted at /api/auth/ by config/urls.py.

Two routes and no ids in either: both address "whoever holds this token", and
an id in the URL would be a second, weaker way to say the same thing.
"""

from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    # Called right after the Firebase OTP succeeds. POST, not GET: it creates
    # the account and its portfolio on a first login, which is not something a
    # safe method may do.
    path("session/", views.session, name="session"),
    path("me/", views.me, name="me"),
]
