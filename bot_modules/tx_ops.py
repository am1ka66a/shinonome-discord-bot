import typing


def lock_user_rows(c, user_ids: typing.Iterable[typing.Union[int, str]]) -> typing.List[str]:
    """Lock users rows in deterministic order to reduce deadlocks."""
    locked: typing.List[str] = []
    normalized = sorted({str(uid) for uid in user_ids if uid is not None})
    for uid in normalized:
        c.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE", (uid,))
        if c.fetchone():
            locked.append(uid)
    return locked


def get_locked_user_balance(c, user_id: typing.Union[int, str]) -> int:
    """Lock and read latest user balance inside current transaction."""
    c.execute(
        "SELECT COALESCE(balance,0) FROM users WHERE user_id=%s FOR UPDATE",
        (str(user_id),),
    )
    row = c.fetchone()
    return int((row[0] if row else 0) or 0)


def change_balance(c, user_id, delta, reason, *, require_funds=False, create=False) -> bool:
    """The only place that moves a balance: update it and write both ledgers together.

    Runs inside the caller's transaction. ``require_funds`` turns a debit into a
    conditional one (``balance >= amount``); ``create`` makes a missing account
    first. Returns False, writing nothing, when the account is missing or short.
    """
    delta = int(delta or 0)
    uid = str(user_id)
    if delta == 0:
        return True
    if create:
        c.execute("INSERT IGNORE INTO users (user_id, balance) VALUES (%s, 0)", (uid,))
    if delta < 0 and require_funds:
        c.execute(
            "UPDATE users SET balance=COALESCE(balance,0)+%s WHERE user_id=%s AND COALESCE(balance,0) >= %s",
            (delta, uid, -delta),
        )
    else:
        c.execute("UPDATE users SET balance=COALESCE(balance,0)+%s WHERE user_id=%s", (delta, uid))
    if c.rowcount == 0:
        return False
    log_transaction_in_tx(c, uid, delta, reason)
    return True


def log_transaction_in_tx(c, user_id, amount, reason):
    """Write logs + casino_logs in the same transaction."""
    amount = int(amount or 0)
    if amount == 0:
        return
    c.execute(
        "INSERT INTO logs (user_id, amount, reason) VALUES (%s, %s, %s)",
        (str(user_id), amount, reason),
    )
    new_log_id = c.lastrowid
    c.execute(
        "INSERT INTO casino_logs (user_id, amount, reason, source_log_id) VALUES (%s, %s, %s, %s)",
        (str(user_id), amount, reason, new_log_id),
    )
