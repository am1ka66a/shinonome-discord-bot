import typing

from bot_modules.db import get_db_connection

RR_LEADERBOARD_MIN_GAMES = 3


def init_rr_stats_table(cursor) -> None:
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS russian_roulette_stats (
            user_id VARCHAR(255) PRIMARY KEY,
            games INT DEFAULT 0,
            wins INT DEFAULT 0,
            profit BIGINT DEFAULT 0
        )"""
    )


def record_rr_result_sync(user_id: int, *, is_win: bool, profit_delta: int) -> None:
    uid = str(user_id)
    win_inc = 1 if is_win else 0
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """INSERT INTO russian_roulette_stats (user_id, games, wins, profit)
           VALUES (%s, 1, %s, %s)
           ON DUPLICATE KEY UPDATE
           games = games + 1,
           wins = wins + %s,
           profit = profit + %s""",
        (uid, win_inc, int(profit_delta), win_inc, int(profit_delta)),
    )
    conn.commit()
    conn.close()


def fetch_rr_stats_sync(user_id: int) -> typing.Dict[str, typing.Any]:
    uid = str(user_id)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT games, wins, profit FROM russian_roulette_stats WHERE user_id=%s",
        (uid,),
    )
    row = c.fetchone()
    games = int(row[0] or 0) if row else 0
    wins = int(row[1] or 0) if row else 0
    profit = int(row[2] or 0) if row else 0
    losses = max(0, games - wins)
    win_rate = (wins * 100.0 / games) if games > 0 else 0.0

    win_rank = None
    rate_rank = None
    if games > 0:
        c.execute(
            "SELECT COUNT(*) + 1 FROM russian_roulette_stats WHERE games > 0 AND wins > %s",
            (wins,),
        )
        win_rank = int((c.fetchone() or [1])[0])
    if games >= RR_LEADERBOARD_MIN_GAMES:
        c.execute(
            """SELECT COUNT(*) + 1 FROM russian_roulette_stats
               WHERE games >= %s
                 AND (wins * 1.0 / games > %s
                      OR (wins * 1.0 / games = %s AND games > %s)
                      OR (wins * 1.0 / games = %s AND games = %s AND wins > %s))""",
            (
                RR_LEADERBOARD_MIN_GAMES,
                win_rate / 100.0,
                win_rate / 100.0,
                games,
                win_rate / 100.0,
                games,
                wins,
            ),
        )
        rate_rank = int((c.fetchone() or [1])[0])
    conn.close()
    return {
        "games": games,
        "wins": wins,
        "losses": losses,
        "profit": profit,
        "win_rate": win_rate,
        "win_rank": win_rank,
        "rate_rank": rate_rank,
    }


def fetch_rr_leaderboard_sync(
    *,
    limit: int = 10,
    min_games: int = RR_LEADERBOARD_MIN_GAMES,
) -> typing.List[typing.Dict[str, typing.Any]]:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT user_id, games, wins, profit
           FROM russian_roulette_stats
           WHERE games >= %s
           ORDER BY wins DESC, (wins * 1.0 / games) DESC, profit DESC
           LIMIT %s""",
        (int(min_games), int(limit)),
    )
    rows = c.fetchall() or []
    conn.close()
    out: typing.List[typing.Dict[str, typing.Any]] = []
    for user_id, games, wins, profit in rows:
        games_i = int(games or 0)
        wins_i = int(wins or 0)
        out.append(
            {
                "user_id": str(user_id),
                "games": games_i,
                "wins": wins_i,
                "losses": max(0, games_i - wins_i),
                "profit": int(profit or 0),
                "win_rate": (wins_i * 100.0 / games_i) if games_i > 0 else 0.0,
            }
        )
    return out
