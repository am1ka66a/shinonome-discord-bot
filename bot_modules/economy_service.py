import typing

from bot_modules.db import db_conn, db_cursor
from bot_modules.tx_ops import change_balance


def debit_user(
    get_db_connection,
    get_locked_user_balance,
    log_transaction_in_tx,
    user_id: typing.Union[int, str],
    amount: int,
    reason: str,
    require_casino_open: bool = False,
) -> bool:
    """Single-user debit with row lock, non-negative invariant, and transaction log."""
    amount = int(amount or 0)
    if amount <= 0:
        return True
    with db_conn(get_db_connection) as conn:
        c = conn.cursor()
        if require_casino_open:
            from bot_modules.maintenance import casino_open
            if not casino_open(c):
                return False
        balance = get_locked_user_balance(c, user_id)
        if balance < amount:
            return False
        if not change_balance(c, user_id, -amount, reason, require_funds=True):
            return False
        conn.commit()
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
    with db_cursor(commit=True, connect=get_db_connection) as c:
        change_balance(c, user_id, amount, reason, create=True)


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
    with db_conn(get_db_connection) as conn:
        c = conn.cursor()
        lock_user_rows(c, [sender, receiver])
        sender_before = get_locked_user_balance(c, sender)
        receiver_before = get_locked_user_balance(c, receiver)
        if sender_before < amount:
            return {"ok": False, "reason": "insufficient"}
        if not change_balance(c, sender, -amount, out_reason, require_funds=True):
            return {"ok": False, "reason": "insufficient"}
        change_balance(c, receiver, amount, in_reason, create=True)
        conn.commit()
    return {
        "ok": True,
        "sender_after": sender_before - amount,
        "receiver_after": receiver_before + amount,
    }
