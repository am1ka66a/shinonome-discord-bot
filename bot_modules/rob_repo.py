import json
import random
import typing


def append_rob_history_on_cursor(c, user_id: int, steal_amount: int, now_tw_naive_func) -> None:
    """Update robber's latest 5 successful robbery records in one transaction."""
    uid_str = str(user_id)
    c.execute("SELECT last_five_robs FROM users WHERE user_id=%s", (uid_str,))
    row = c.fetchone()
    history: typing.List[typing.Any] = []
    raw = row[0] if row else None
    if raw:
        try:
            parsed = json.loads(raw)
            history = parsed if isinstance(parsed, list) else []
        except Exception:
            history = []
    history.append(
        {
            "amount": int(steal_amount),
            "time": now_tw_naive_func().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    history = history[-5:]
    c.execute(
        "UPDATE users SET last_five_robs=%s WHERE user_id=%s",
        (json.dumps(history, ensure_ascii=False), uid_str),
    )


def get_last_five_robs_total(get_db_connection, user_id: typing.Union[int, str]) -> typing.Tuple[int, int, typing.List[typing.Any]]:
    uid = str(user_id)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT last_five_robs FROM users WHERE user_id=%s", (uid,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return 0, 0, []
    try:
        history = json.loads(row[0])
    except Exception:
        return 0, 0, []
    if not isinstance(history, list):
        return 0, 0, []
    total = sum(int(item.get("amount", 0) or 0) for item in history if isinstance(item, dict))
    return total, len(history), history


def rob_history_total_from_raw(raw: typing.Any) -> typing.Tuple[int, int]:
    if not raw:
        return 0, 0
    try:
        history = json.loads(raw)
    except Exception:
        return 0, 0
    if not isinstance(history, list):
        return 0, 0
    total = sum(int(item.get("amount", 0) or 0) for item in history if isinstance(item, dict))
    return total, len(history)


def clear_rob_history(get_db_connection, user_id: typing.Union[int, str]) -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET last_five_robs=NULL WHERE user_id=%s", (str(user_id),))
    conn.commit()
    conn.close()


def load_rob_context(get_db_connection, lock_user_rows, user_role_value_func, robber_id: int, target_id: int) -> typing.Dict[str, typing.Any]:
    conn = get_db_connection()
    c = conn.cursor()
    lock_user_rows(c, [robber_id, target_id])
    c.execute(
        "SELECT COALESCE(in_prison,0), COALESCE(role,'civilian'), balance, last_rob, level FROM users WHERE user_id=%s FOR UPDATE",
        (str(robber_id),),
    )
    robber_row = c.fetchone() or (0, "civilian", 0, None, 1)
    c.execute(
        "SELECT balance, level, last_robbed, COALESCE(good_citizen_cert_active,0) FROM users WHERE user_id=%s FOR UPDATE",
        (str(target_id),),
    )
    target_row = c.fetchone() or (0, 1, None, 0)
    conn.close()
    return {
        "in_prison": int(robber_row[0] or 0),
        "robber_role": user_role_value_func(robber_row[1]),
        "robber_balance": int(robber_row[2] or 0),
        "last_rob": robber_row[3],
        "robber_level": int(robber_row[4] or 1),
        "target_balance": int(target_row[0] or 0),
        "target_level": int(target_row[1] or 1),
        "target_last_robbed": target_row[2],
        "target_good_cert": int(target_row[3] or 0),
    }


def apply_rob_success_db(
    get_db_connection,
    lock_user_rows,
    now_tw_naive_func,
    user_role_value_func,
    cop_hunt_capture_base_pct: int,
    cop_hunt_capture_per_star_pct: int,
    robber_id: int,
    target_id: int,
    now,
    success_rate_pct: int,
) -> typing.Dict[str, typing.Any]:
    conn = get_db_connection()
    c = conn.cursor()
    lock_user_rows(c, [robber_id, target_id])
    c.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=%s FOR UPDATE", (str(target_id),))
    trow = c.fetchone()
    target_balance_snapshot = int((trow[0] if trow else 0) or 0)
    c.execute("UPDATE users SET last_rob=%s WHERE user_id=%s", (now, str(robber_id)))
    steal_amount = int(max(1, min(target_balance_snapshot * random.uniform(0.10, 0.25), 1_000_000)))
    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
        (steal_amount, str(target_id), steal_amount),
    )
    if c.rowcount == 0:
        conn.commit()
        conn.close()
        return {"ok": False, "reason": "target_hidden"}

    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (steal_amount, str(robber_id)))
    c.execute("UPDATE users SET last_robbed=%s WHERE user_id=%s", (now, str(target_id)))
    append_rob_history_on_cursor(c, robber_id, steal_amount, now_tw_naive_func)

    wanted_info = ""
    c.execute("SELECT COALESCE(wanted_stars,0) FROM users WHERE user_id=%s", (str(robber_id),))
    ws = c.fetchone()
    current_wanted = int(ws[0] or 0) if ws else 0
    if current_wanted < 5:
        c.execute(
            "UPDATE users SET wanted_stars=LEAST(5, COALESCE(wanted_stars,0)+1), wanted_hunted_count=0 WHERE user_id=%s",
            (str(robber_id),),
        )
        c.execute("SELECT COALESCE(wanted_stars,0) FROM users WHERE user_id=%s", (str(robber_id),))
        nw = c.fetchone()
        new_wanted = int(nw[0] or 0) if nw else 0
        stars_display = "⭐" * new_wanted + "☆" * (5 - new_wanted)
        if new_wanted == 5:
            wanted_info = (
                f"\n🔴 **達到最高通緝！** {stars_display}\n"
                f"⚠️ 每次搶劫成功後警察可追捕一次。"
            )
        else:
            cap = min(95, cop_hunt_capture_base_pct + new_wanted * cop_hunt_capture_per_star_pct)
            wanted_info = (
                f"\n⚠️ **通緝等級提升** → {stars_display}（{new_wanted}/5）\n"
                f"🚔 追捕成功率基準約：**{cap}%**（實際另受警匪等級差影響，每級 ±1%）"
            )
    else:
        c.execute("UPDATE users SET wanted_hunted_count=0 WHERE user_id=%s", (str(robber_id),))
        cap5 = min(95, cop_hunt_capture_base_pct + 5 * cop_hunt_capture_per_star_pct)
        wanted_info = (
            f"\n🔴 **滿星通緝中** ⭐⭐⭐⭐⭐\n"
            f"🚔 每次搶劫成功後警察可追捕一次（基準約 **`{cap5}%`**，實際另受警匪等級差影響，每級 ±1%）。"
        )

    revenge_hint = ""
    c.execute("SELECT COALESCE(role,'civilian') FROM users WHERE user_id=%s", (str(target_id),))
    vrole_row = c.fetchone()
    victim_role = user_role_value_func(vrole_row[0] if vrole_row else None)
    if victim_role == "civilian":
        c.execute(
            "UPDATE users SET revenge_pending=1, revenge_robber_id=%s, revenge_amount=%s WHERE user_id=%s",
            (str(robber_id), steal_amount, str(target_id)),
        )
        revenge_hint = (
            f"\n\n💢 <@{target_id}> 身為**平民**被搶成功，獲得 **一次**機會：使用 `/counter_rob` "
            f"可依每級差距 ±1% 公式（與搶劫相同結構，基礎成功率較低），嘗試從搶匪處**加倍搶回**（至多 `{(steal_amount * 2):,}` 幣，實際以搶匪餘額為準）。"
        )

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "steal_amount": steal_amount,
        "wanted_info": wanted_info,
        "revenge_hint": revenge_hint,
        "success_rate_pct": success_rate_pct,
    }


def apply_rob_fail_db(get_db_connection, lock_user_rows, robber_id: int, target_id: int, now) -> typing.Dict[str, typing.Any]:
    conn = get_db_connection()
    c = conn.cursor()
    lock_user_rows(c, [robber_id, target_id])
    c.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=%s FOR UPDATE", (str(robber_id),))
    rrow = c.fetchone()
    robber_balance_snapshot = int((rrow[0] if rrow else 0) or 0)
    c.execute("UPDATE users SET last_rob=%s WHERE user_id=%s", (now, str(robber_id)))
    fail_penalty = int(max(1, min(robber_balance_snapshot * random.uniform(0.15, 0.45), 1_000_000)))
    deducted = False
    if fail_penalty > 0:
        c.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
            (fail_penalty, str(robber_id), fail_penalty),
        )
        deducted = c.rowcount > 0
        if deducted:
            c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (fail_penalty, str(target_id)))
    conn.commit()
    conn.close()
    return {"fail_penalty": fail_penalty, "deducted": deducted}
