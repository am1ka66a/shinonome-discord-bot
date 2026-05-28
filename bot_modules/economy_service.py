import typing


def debit_user(
    get_db_connection,
    get_locked_user_balance,
    log_transaction_in_tx,
    user_id: typing.Union[int, str],
    amount: int,
    reason: str,
) -> bool:
    """Single-user debit with row lock, non-negative invariant, and transaction log."""
    amount = int(amount or 0)
    if amount <= 0:
        return True
    conn = get_db_connection()
    c = conn.cursor()
    balance = get_locked_user_balance(c, user_id)
    if balance < amount:
        conn.close()
        return False
    c.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s", (amount, str(user_id)))
    log_transaction_in_tx(c, user_id, -amount, reason)
    conn.commit()
    conn.close()
    return True


def credit_user(
    get_db_connection,
    log_transaction_in_tx,
    user_id: typing.Union[int, str],
    amount: int,
    reason: str,
) -> None:
    """Single-user credit with row lock and transaction log."""
    amount = int(amount or 0)
    if amount <= 0:
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
    c.fetchone()
    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, str(user_id)))
    log_transaction_in_tx(c, user_id, amount, reason)
    conn.commit()
    conn.close()


def transfer_users(
    get_db_connection,
    lock_user_rows,
    get_locked_user_balance,
    log_transaction_in_tx,
    sender_id: typing.Union[int, str],
    receiver_id: typing.Union[int, str],
    amount: int,
    out_reason: str,
    in_reason: str,
) -> typing.Dict[str, typing.Any]:
    """Two-party transfer with deterministic row locking and paired logs."""
    amount = int(amount or 0)
    if amount <= 0:
        return {"ok": False, "reason": "invalid_amount"}
    sender = str(sender_id)
    receiver = str(receiver_id)
    conn = get_db_connection()
    c = conn.cursor()
    lock_user_rows(c, [sender, receiver])
    sender_before = get_locked_user_balance(c, sender)
    receiver_before = get_locked_user_balance(c, receiver)
    if sender_before < amount:
        conn.close()
        return {"ok": False, "reason": "insufficient"}
    c.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s", (amount, sender))
    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, receiver))
    log_transaction_in_tx(c, sender, -amount, out_reason)
    log_transaction_in_tx(c, receiver, amount, in_reason)
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "sender_after": sender_before - amount,
        "receiver_after": receiver_before + amount,
    }
