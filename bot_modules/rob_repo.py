import json
import random
import typing

from bot_modules.db import db_conn, db_cursor


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
    with db_cursor(connect=get_db_connection) as c:
        c.execute("SELECT last_five_robs FROM users WHERE user_id=%s", (uid,))
        row = c.fetchone()
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
    with db_cursor(commit=True, connect=get_db_connection) as c:
        c.execute("UPDATE users SET last_five_robs=NULL WHERE user_id=%s", (str(user_id),))


def load_rob_context(get_db_connection, lock_user_rows, user_role_value_func, robber_id: int, target_id: int) -> typing.Dict[str, typing.Any]:
    with db_cursor(connect=get_db_connection) as c:
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
    log_transaction_in_tx,
    counter_rob_base_success_rate: float,
    bail_cost: int,
    rob_steal_cap: int,
    cop_hunt_capture_base_pct: int,
    cop_hunt_capture_per_star_pct: int,
    robber_id: int,
    target_id: int,
    now,
    success_rate_pct: int,
) -> typing.Dict[str, typing.Any]:
    with db_conn(get_db_connection) as conn:
        c = conn.cursor()
        lock_user_rows(c, [robber_id, target_id])
        c.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=%s FOR UPDATE", (str(target_id),))
        trow = c.fetchone()
        target_balance_snapshot = int((trow[0] if trow else 0) or 0)
        c.execute("UPDATE users SET last_rob=%s WHERE user_id=%s", (now, str(robber_id)))
        steal_amount = int(max(1, min(target_balance_snapshot * random.uniform(0.10, 0.25), int(rob_steal_cap))))
        c.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
            (steal_amount, str(target_id), steal_amount),
        )
        if c.rowcount == 0:
            conn.commit()
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

        counter_note = ""
        c.execute("SELECT COALESCE(role,'civilian') FROM users WHERE user_id=%s", (str(target_id),))
        vrole_row = c.fetchone()
        victim_role = user_role_value_func(vrole_row[0] if vrole_row else None)
        if victim_role == "civilian":
            c.execute(
                "SELECT COALESCE(level,1), COALESCE(balance,0) FROM users WHERE user_id=%s FOR UPDATE",
                (str(target_id),),
            )
            victim_row = c.fetchone()
            victim_level = int((victim_row[0] if victim_row else 1) or 1)
            c.execute(
                "SELECT COALESCE(level,1), COALESCE(balance,0), COALESCE(in_prison,0) FROM users WHERE user_id=%s FOR UPDATE",
                (str(robber_id),),
            )
            robber_row = c.fetchone()
            robber_level = int((robber_row[0] if robber_row else 1) or 1)
            robber_balance = int((robber_row[1] if robber_row else 0) or 0)
            already_prison = int((robber_row[2] if robber_row else 0) or 0)

            level_gap = victim_level - robber_level
            counter_rate = counter_rob_base_success_rate + (level_gap * 0.01)
            counter_rate = max(0.05, min(0.95, counter_rate))
            counter_pct = int(round(counter_rate * 100))
            doubled = int(steal_amount * 2)

            if random.random() < counter_rate:
                pay = min(doubled, robber_balance)
                debt_from_shortfall = max(0, doubled - pay)
                c.execute(
                    "UPDATE users SET balance=GREATEST(0, balance-%s), bail_debt=COALESCE(bail_debt,0)+%s WHERE user_id=%s",
                    (pay, debt_from_shortfall, str(robber_id)),
                )
                c.execute(
                    "SELECT COALESCE(balance,0), COALESCE(in_prison,0) FROM users WHERE user_id=%s FOR UPDATE",
                    (str(robber_id),),
                )
                robber_after = c.fetchone()
                robber_balance_after = int((robber_after[0] if robber_after else 0) or 0)
                robber_in_prison_after = int((robber_after[1] if robber_after else 0) or 0)
                robber_imprisoned = False
                if robber_balance_after == 0 and not already_prison and not robber_in_prison_after:
                    prison_now = now_tw_naive_func()
                    c.execute(
                        "UPDATE users SET in_prison=1, prison_start=%s WHERE user_id=%s AND COALESCE(in_prison,0)=0",
                        (prison_now, str(robber_id)),
                    )
                    c.execute(
                        """INSERT INTO prison_records
                       (criminal_id, cop_id, wanted_stars, confiscated_amount, cop_reward, bail_cost, arrested_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (str(robber_id), str(target_id), 0, pay, 0, int(bail_cost), prison_now),
                    )
                    robber_imprisoned = True
                c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (doubled, str(target_id)))
                log_transaction_in_tx(c, target_id, doubled, f"平民自動反制成功（對搶匪:{robber_id}）")
                if pay > 0:
                    log_transaction_in_tx(c, robber_id, -pay, f"被平民自動反制（受害者:{target_id}）")
                debt_note = ""
                if debt_from_shortfall > 0:
                    debt_note = f"；搶匪餘額不足，差額 `{debt_from_shortfall:,}` 已記入假釋債務"
                prison_note = "；搶匪餘額歸零，已入獄" if robber_imprisoned else ""
                counter_note = (
                    f"\n💢 平民自動反制**成功**（機率約 {counter_pct}%）："
                    f"當場加倍搶回 `{doubled:,}`，搶匪實扣 `{pay:,}`{debt_note}{prison_note}。"
                )
            else:
                counter_note = (
                    f"\n💢 平民自動反制**失敗**（機率約 {counter_pct}%），本次未追回。"
                )
            c.execute(
                "UPDATE users SET revenge_pending=0, revenge_robber_id=NULL, revenge_amount=0 WHERE user_id=%s",
                (str(target_id),),
            )

        conn.commit()
    return {
        "ok": True,
        "steal_amount": steal_amount,
        "wanted_info": wanted_info,
        "counter_note": counter_note,
        "success_rate_pct": success_rate_pct,
    }


def apply_rob_fail_db(
    get_db_connection,
    lock_user_rows,
    rob_fail_penalty_cap: int,
    robber_id: int,
    target_id: int,
    now,
) -> typing.Dict[str, typing.Any]:
    with db_cursor(commit=True, connect=get_db_connection) as c:
        lock_user_rows(c, [robber_id, target_id])
        c.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=%s FOR UPDATE", (str(robber_id),))
        rrow = c.fetchone()
        robber_balance_snapshot = int((rrow[0] if rrow else 0) or 0)
        c.execute("UPDATE users SET last_rob=%s WHERE user_id=%s", (now, str(robber_id)))
        fail_penalty = int(max(1, min(robber_balance_snapshot * random.uniform(0.15, 0.45), int(rob_fail_penalty_cap))))
        deducted = False
        if fail_penalty > 0:
            c.execute(
                "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
                (fail_penalty, str(robber_id), fail_penalty),
            )
            deducted = c.rowcount > 0
            if deducted:
                c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (fail_penalty, str(target_id)))
    return {"fail_penalty": fail_penalty, "deducted": deducted}
