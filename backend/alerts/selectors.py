"""
Alert reads (architecture rule 1).

TODO Phase 6:
    active_rules() -> QuerySet[AlertRule]                    # select_related portfolio
    recent_events(portfolio_id, limit=50) -> QuerySet[AlertEvent]
    unacknowledged_count(portfolio_id) -> int
"""
