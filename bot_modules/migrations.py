"""Versioned, additive migrations. Existing ledger rows are never rebuilt."""
import pymysql


BASE_TABLES = (
    """CREATE TABLE IF NOT EXISTS users
        (user_id VARCHAR(255) PRIMARY KEY, balance BIGINT, rescue_count INT DEFAULT 0,
        total_games INT DEFAULT 0, wins INT DEFAULT 0, total_profit BIGINT DEFAULT 0,
        last_work TIMESTAMP NULL, last_beg TIMESTAMP NULL, last_rescue TIMESTAMP NULL, last_rob TIMESTAMP NULL, last_robbed TIMESTAMP NULL,
        exp BIGINT DEFAULT 0, level INT DEFAULT 1,
        last_hourly_claim TIMESTAMP NULL, hourly_bank INT DEFAULT 0,
        good_citizen_cert_active TINYINT(1) DEFAULT 0, last_good_citizen_cert_action TIMESTAMP NULL,
    good_citizen_cert_broken_until TIMESTAMP NULL)""",
    """CREATE TABLE IF NOT EXISTS wanted_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        criminal_id VARCHAR(255) NOT NULL,
        cop_id VARCHAR(255) NOT NULL,
        wanted_stars INT NOT NULL,
        caught TINYINT(1) DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS prison_records (
        id INT AUTO_INCREMENT PRIMARY KEY,
        criminal_id VARCHAR(255) NOT NULL,
        cop_id VARCHAR(255) NOT NULL,
        wanted_stars INT NOT NULL,
        confiscated_amount BIGINT NOT NULL,
        cop_reward BIGINT NOT NULL,
        bail_cost BIGINT DEFAULT 100000,
        arrested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        released_at TIMESTAMP NULL
    )""",
    """CREATE TABLE IF NOT EXISTS activity_stats
        (user_id VARCHAR(255) PRIMARY KEY, msg_count INT DEFAULT 0,
        last_msg_reward TIMESTAMP NULL, last_vc_reward TIMESTAMP NULL,
    last_exp_reward TIMESTAMP NULL)""",
    'CREATE TABLE IF NOT EXISTS blacklist (user_id VARCHAR(255) PRIMARY KEY)',
    'CREATE TABLE IF NOT EXISTS daily_claims (user_id VARCHAR(255) PRIMARY KEY, last_claim DATE)',
    'CREATE TABLE IF NOT EXISTS logs (id INT AUTO_INCREMENT PRIMARY KEY, user_id VARCHAR(255), amount BIGINT, reason VARCHAR(255), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)',
    """CREATE TABLE IF NOT EXISTS casino_logs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id VARCHAR(255),
        amount BIGINT,
        reason VARCHAR(255),
        source_log_id INT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
)

ADDITIVE_COLUMNS = (
    'ALTER TABLE users ADD COLUMN last_work TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN last_beg TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN last_rescue TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN last_rob TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN last_robbed TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN exp BIGINT DEFAULT 0',
    'ALTER TABLE users ADD COLUMN level INT DEFAULT 1',
    'ALTER TABLE users ADD COLUMN last_hourly_claim TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN hourly_bank INT DEFAULT 0',
    "ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'civilian'",
    'ALTER TABLE users ADD COLUMN wanted_stars INT DEFAULT 0',
    'ALTER TABLE users ADD COLUMN wanted_hunted_count INT DEFAULT 0',
    'ALTER TABLE users ADD COLUMN last_five_robs TEXT NULL',
    'ALTER TABLE users ADD COLUMN in_prison TINYINT(1) DEFAULT 0',
    'ALTER TABLE users ADD COLUMN prison_start TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN arrest_count INT DEFAULT 0',
    'ALTER TABLE users ADD COLUMN revenge_pending TINYINT(1) DEFAULT 0',
    'ALTER TABLE users ADD COLUMN revenge_robber_id VARCHAR(255) NULL',
    'ALTER TABLE users ADD COLUMN revenge_amount BIGINT DEFAULT 0',
    'ALTER TABLE users ADD COLUMN last_wanted_buyout TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN last_bicycle TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN last_role_change TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN bail_debt BIGINT DEFAULT 0',
    'ALTER TABLE users ADD COLUMN good_citizen_cert_active TINYINT(1) DEFAULT 0',
    'ALTER TABLE users ADD COLUMN last_good_citizen_cert_action TIMESTAMP NULL',
    'ALTER TABLE users ADD COLUMN good_citizen_cert_broken_until TIMESTAMP NULL',
    'ALTER TABLE activity_stats ADD COLUMN last_exp_reward TIMESTAMP NULL',
    'ALTER TABLE casino_logs ADD COLUMN source_log_id INT NULL',
)

INDEXES = (
    'CREATE INDEX idx_logs_user_created ON logs (user_id, created_at)',
    'CREATE UNIQUE INDEX uq_casino_logs_source_log_id ON casino_logs (source_log_id)',
    'CREATE INDEX idx_casino_logs_user_created ON casino_logs (user_id, created_at)',
    'CREATE INDEX idx_users_level_exp ON users (level, exp)',
)


def baseline_schema(c):
    from bot_modules.state_repo import init_state_tables
    from bot_modules.lottery_repo import init_lottery_tables
    from bot_modules.rr_repo import init_rr_stats_table
    from bot_modules.rr_match_repo import init_rr_match_table
    init_state_tables(c)
    for sql in BASE_TABLES:
        c.execute(sql)
    for statements, duplicate_code in ((ADDITIVE_COLUMNS, 1060), (INDEXES, 1061)):
        for sql in statements:
            try:
                c.execute(sql)
            except pymysql.MySQLError as error:
                if not error.args or error.args[0] != duplicate_code:
                    raise
    init_lottery_tables(c)
    init_rr_stats_table(c)
    init_rr_match_table(c)
    # Seed only an empty ledger; old records without source IDs are preserved.
    c.execute("SELECT COUNT(*) FROM casino_logs")
    if int(c.fetchone()[0]) == 0:
        c.execute("""INSERT INTO casino_logs (user_id, amount, reason, source_log_id, created_at)
            SELECT user_id, amount, reason, id, created_at FROM logs""")


def maintenance_tables(c):
    c.execute("""CREATE TABLE IF NOT EXISTS bot_admin_operations (
        request_id VARCHAR(96) PRIMARY KEY, actor_id VARCHAR(255) NOT NULL,
        action VARCHAR(32) NOT NULL, result_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_admin_snapshots (
        request_id VARCHAR(96) NOT NULL, user_id VARCHAR(255) NOT NULL,
        snapshot_json TEXT NOT NULL, PRIMARY KEY(request_id,user_id))""")


def audit_indexes(c):
    for sql in (
        'CREATE INDEX idx_casino_logs_user_id ON casino_logs (user_id, id)',
        'CREATE INDEX idx_admin_operations_created ON bot_admin_operations (created_at, request_id)',
    ):
        try:
            c.execute(sql)
        except pymysql.MySQLError as error:
            if not error.args or error.args[0] != 1061:
                raise


MIGRATIONS = ((1, baseline_schema), (2, maintenance_tables), (3, audit_indexes))


def run_migrations(conn):
    c = conn.cursor()
    c.execute("SELECT GET_LOCK('shinonome:schema',30)")
    if c.fetchone()[0] != 1:
        raise RuntimeError("另一個程序正在升級資料庫，請稍後重試")
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS bot_schema_migrations (
            version INT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("SELECT version FROM bot_schema_migrations")
        applied = {int(row[0]) for row in c.fetchall()}
        for version, migrate in MIGRATIONS:
            if version in applied:
                continue
            migrate(c)
            c.execute("INSERT INTO bot_schema_migrations (version) VALUES (%s)", (version,))
            conn.commit()
    finally:
        c.execute("SELECT RELEASE_LOCK('shinonome:schema')")
