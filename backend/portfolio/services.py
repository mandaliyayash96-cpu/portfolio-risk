"""
Portfolio writes (architecture rule 1: views never touch the ORM).

Every create/update/delete for Portfolio, Holding and Transaction lands here,
raises common.exceptions.DomainError subclasses on business-rule failures, and
returns model instances or plain dicts.

TODO Phase 4:
    create_portfolio(user, name, base_currency) -> Portfolio
    add_holding(portfolio_id, ticker, quantity, avg_buy_price, buy_date, ...) -> Holding
    record_transaction(portfolio_id, ticker, side, quantity, price, timestamp) -> Transaction
        - wrap in transaction.atomic()
        - recompute the affected Holding's quantity + avg_buy_price from the ledger
        - reject a SELL that exceeds the held quantity (raise InvalidInputError)
    delete_holding(portfolio_id, ticker) -> None
"""
