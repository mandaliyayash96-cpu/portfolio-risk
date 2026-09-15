"""
AI insight endpoints, mounted at /api/insights/ by config/urls.py.

One POST. There is no GET that returns the last set of insights: nothing is
stored, every call is a fresh look at the current numbers, and a cached answer
that could disagree with the report beside it would be worse than none.
"""

from django.urls import path

from insights import views

app_name = "insights"

urlpatterns = [
    path("<int:portfolio_id>/", views.ai_insights, name="ai-insights"),
]
