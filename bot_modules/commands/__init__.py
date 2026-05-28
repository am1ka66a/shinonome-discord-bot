from .blackjack import register_blackjack_commands
from .duel import register_duel_commands
from .economy import register_economy_commands
from .fun import register_fun_commands
from .stats import register_stats_commands
from .wanted import register_wanted_commands

__all__ = [
    "register_blackjack_commands",
    "register_duel_commands",
    "register_economy_commands",
    "register_fun_commands",
    "register_stats_commands",
    "register_wanted_commands",
]
