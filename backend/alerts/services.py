"""
Alert writes (architecture rule 1).

TODO Phase 6:
    create_rule(portfolio_id, metric, operator, threshold) -> AlertRule
    evaluate_rule(rule, current_value) -> bool          # pure comparison on the operator
    scan_alerts() -> list[AlertEvent]                   # Celery Beat entry point:
        - recompute each active rule's metric (risk.services / marketdata.selectors)
        - on breach create an AlertEvent
        - push it over the Channels group for that portfolio
    acknowledge(event_id) -> AlertEvent
"""
