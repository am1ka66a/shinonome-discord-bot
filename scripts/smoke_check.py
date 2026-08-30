"""Smoke checks for modularized bot (no Discord connection)."""
import ast
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

errors: typing.List[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"OK  {name}")
    except Exception as e:
        errors.append(f"{name}: {e}")
        print(f"FAIL {name}: {e}")


def import_modules() -> None:
    from bot_modules import (
        assembly,
        async_bridge,
        bot_core,
        config,
        discord_helpers,
        discord_logging,
        domain_sync,
        level_rewards,
    )
    from bot_modules.runtime import app_lock, relay, snapshot_cache, events
    from bot_modules.commands import (
        register_blackjack_commands,
        register_casino_light_commands,
        register_duel_commands,
        register_economy_commands,
        register_fun_commands,
        register_social_commands,
        register_stats_commands,
        register_wanted_commands,
    )
    assert assembly.register_all_commands
    assert bot_core.create_shinonome_bot
    assert domain_sync.claim_daily_reward
    assert async_bridge.build_async_wrappers(domain_sync)["db_to_thread"]


def async_bridge_keys() -> None:
    from bot_modules import domain_sync
    from bot_modules.async_bridge import build_async_wrappers

    required = {
        "db_to_thread",
        "ensure_user_exists_async",
        "transfer_sync_async",
        "credit_balance_with_log_async",
        "settle_duel_payouts_with_log_async",
    }
    got = set(build_async_wrappers(domain_sync))
    missing = required - got
    if missing:
        raise AssertionError(f"missing async wrappers: {missing}")


def bot_py_ast() -> None:
    src = open(ROOT / "bot.py", encoding="utf-8").read()
    ast.parse(src)
    if "create_shinonome_bot(" not in src:
        raise AssertionError("bot.py missing create_shinonome_bot()")
    if "register_on_ready(" not in src:
        raise AssertionError("bot.py missing register_on_ready()")
    if "assembly.register_all_commands(" not in src:
        raise AssertionError("bot.py missing assembly.register_all_commands()")
    if "bot.run(" not in src:
        raise AssertionError("bot.py missing bot.run()")


def bot_py_ctx_names() -> None:
    from bot_modules import domain_sync
    from bot_modules.async_bridge import build_async_wrappers

    src = open(ROOT / "bot.py", encoding="utf-8").read()
    tree = ast.parse(src)
    ctx_keys: typing.Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "register_all_commands":
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Dict):
            raise AssertionError("register_all_commands second arg must be dict literal")
        for k in node.args[1].keys:
            if isinstance(k, ast.Constant):
                ctx_keys.add(str(k.value))
    pre = src.split("assembly.register_all_commands")[0]
    async_keys = set(build_async_wrappers(domain_sync))
    defined = set(pre.split()) | async_keys
    missing = [k for k in sorted(ctx_keys) if k not in pre and k not in async_keys]
    if missing:
        raise AssertionError(f"ctx keys possibly undefined: {missing}")


def domain_sync_exports() -> None:
    from bot_modules import domain_sync

    for name in (
        "try_deduct_balance",
        "update_game_result",
        "fetch_casino_share_stats_rows",
        "parse_tw_datetime",
        "pay_bail_sync",
    ):
        if not hasattr(domain_sync, name):
            raise AssertionError(f"domain_sync missing {name}")


def assembly_ctx_complete() -> None:
    import re
    import ast

    asm = open(ROOT / "bot_modules" / "assembly.py", encoding="utf-8").read()
    needed = set(re.findall(r'ctx\["([^"]+)"\]', asm))
    tree = ast.parse(open(ROOT / "bot.py", encoding="utf-8").read())
    provided: typing.Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "register_all_commands":
            continue
        for k in node.args[1].keys:
            if isinstance(k, ast.Constant):
                provided.add(str(k.value))
    missing = sorted(needed - provided)
    if missing:
        raise AssertionError(f"bot.py ctx missing keys for assembly: {missing}")


if __name__ == "__main__":
    check("import modules", import_modules)
    check("async_bridge keys", async_bridge_keys)
    check("bot.py AST", bot_py_ast)
    check("bot.py ctx names", bot_py_ctx_names)
    check("domain_sync exports", domain_sync_exports)
    check("assembly ctx complete", assembly_ctx_complete)
    print("---")
    if errors:
        print(f"{len(errors)} check(s) failed")
        sys.exit(1)
    print("All smoke checks passed.")
