from .blackjack import register_blackjack_commands
from .casino_light import register_casino_light_commands
from .duel import register_duel_commands
from .economy import register_economy_commands
from .fun import register_fun_commands
from .social import register_social_commands
from .stats import register_stats_commands
from .wanted import register_wanted_commands

__all__ = [
    "register_blackjack_commands",
    "register_casino_light_commands",
    "register_duel_commands",
    "register_economy_commands",
    "register_fun_commands",
    "register_social_commands",
    "register_stats_commands",
    "register_wanted_commands",
]
