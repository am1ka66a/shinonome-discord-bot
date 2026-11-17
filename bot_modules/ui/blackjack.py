"""Blackjack result text and rendering, without database calls."""
import discord
from bot_modules.games.blackjack import calculate_score, pair_outcome, trio_outcome

OUTCOME_MESSAGES = {
    "five_tie": "🤝 雙方皆過五關！平手",
    "player_five": "🐉 你過五關啦！爽贏 2.5 倍！",
    "dealer_five": "🐉 老子過五關啦！你這低能兒～",
    "blackjack_tie": "🤝 雙方皆為 BlackJack！平手",
    "player_blackjack": "🌟 BlackJack！1.5倍賠率！",
    "dealer_blackjack": "💀 莊家 BlackJack！你輸啦～雜魚～",
    "win": "🎉 這次算你贏啦，腦殘！",
    "lose": "💀 你輸啦～雜魚～",
    "tie": "🤝 就這點技術阿腦殘？",
}
SIDE_NAMES = {"suited_pair": "同花對子", "mixed_pair": "混合對子", "suited_triplet": "同花三條",
              "straight_flush": "同花順", "triplet": "三條", "straight": "順子", "flush": "同花"}


def check_sidebets(player_hand, dealer_up, p_bet, s_bet):
    profit, text = 0, ""
    for amount, resolve, cards, prefix in [
        (p_bet, pair_outcome, player_hand, "🧧 "),
        (s_bet, trio_outcome, [*player_hand[:2], dealer_up], "🎯 21+3 "),
    ]:
        if amount <= 0:
            continue
        kind, multiplier = resolve(cards)
        delta = amount * multiplier
        profit += delta
        if multiplier > 0:
            text += f"{prefix}{SIDE_NAMES[kind]}！+{delta} "
        else:
            text += f"{prefix}{'對子' if prefix.startswith('🧧') else ''}未中 -{amount} "
    return profit, text


def card_to_emoji(card, guild_id=None) -> str:
    return f"**[{card['rank']} {card['suit']}]**"


def card_back_emoji(guild_id=None) -> str:
    return "**[??]**"


def build_game_embed(self, stats, done=False, res="", profit=0, animating=False, extra_msg="", guild_id=None):
    if stats:
        bal, total, wins, t_prof = stats
    else:
        bal, total, wins, t_prof = 0, 0, 0, 0
    wr = (wins / total * 100) if total > 0 else 0
    embed = discord.Embed(title="🃏 21點大賽", color=0x2B2D31)
    main_ui = f"💰 餘額：{bal} | 🏆 勝場：{wins} | 🎲 總局數：{total} | 📈 勝率：{wr:.1f}% | 💸 總盈虧：{t_prof}\n"
    if extra_msg:
        main_ui += f"**{extra_msg}**\n"
    for i, hand in enumerate(self.hands):
        indicator = "👉 " if i == self.current_hand and not done else ""
        title_text = f"{indicator}👤 {self.user.display_name} 的手牌"
        if len(self.hands) > 1:
            title_text += f" (第 {i+1} 手)"
        p_cards = " ".join([card_to_emoji(c, guild_id) for c in hand])
        main_ui += f"### {title_text}\n### {p_cards} (點數: **{calculate_score(hand)}**)\n"
    if done or animating:
        d_cards = " ".join([card_to_emoji(c, guild_id) for c in self.d_hand])
        main_ui += f"### 🤖 莊家手牌\n### {d_cards} (點數: **{calculate_score(self.d_hand)}**)\n"
        if done:
            side_profit = self.side_p
            total_profit = profit + side_profit
            res_line = f"### 🏆 {res}\n{self.side_m}\n"
            side_text = f"+{side_profit}" if side_profit > 0 else str(side_profit)
            res_line += f"🧾 主局淨損益：`{profit:+d}` | 旁注淨損益：`{side_text}`\n"
            if total_profit > 0:
                res_line += f"📈 本局淨損益：`+{total_profit}` | 💰 餘額：`{bal}`\n"
            elif total_profit < 0:
                res_line += f"📉 本局淨損益：`{total_profit}` | 💰 餘額：`{bal}`\n"
            else:
                res_line += f"➖ 本局淨損益：`0` | 💰 餘額：`{bal}`\n"
            main_ui += res_line
    else:
        main_ui += f"### 🤖 莊家手牌\n### {card_to_emoji(self.d_hand[0], guild_id)} {card_back_emoji(guild_id)} (點數: **❓**)\n"
    embed.description = main_ui
    return embed
