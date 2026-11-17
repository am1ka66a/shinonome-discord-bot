"""Roulette presentation; no database access or state mutations."""
import typing

import discord

from bot_modules.games.roulette import (
    hit_probability, royale_bullet_count, royale_chamber_count,
    RR_ROYALE_MIN_PLAYERS, RR_ROYALE_MAX_PLAYERS,
    RR_ROYALE_BASE_PLAYERS, RR_ROYALE_CHAMBERS_PER_EXTRA_PLAYER,
)


def build_embed(
    self,
    interaction: typing.Optional[discord.Interaction] = None,
    *,
    finished: bool = False,
    loser_uid: typing.Optional[int] = None,
    awaiting_choice: bool = False,
    timeout_loss: bool = False,
    winner_uid: typing.Optional[int] = None,
) -> discord.Embed:
    if finished:
        if self.mode == "royale" and winner_uid is not None:
            loss_line = ""
            if loser_uid is not None:
                if timeout_loss:
                    loss_line = f"<@{loser_uid}> 逾時未行動，淘汰。\n"
                else:
                    loss_line = f"<@{loser_uid}> 中彈出局。\n"
            emb = discord.Embed(
                title="💥 俄羅斯輪盤淘汰賽 — 結束",
                description=(
                    f"{loss_line}"
                    f"🏆 最後倖存者 <@{winner_uid}> 贏得 **`{self.pot:,}`** 東雲幣！"
                ),
                color=0xED4245,
            )
        elif loser_uid is not None:
            win_uid = self.other_uid_duel(loser_uid)
            if timeout_loss:
                loss_line = f"<@{loser_uid}> 逾時未扣扳機，視同落敗。"
            else:
                loss_line = f"<@{loser_uid}> 扣下扳機，當場出局。"
            emb = discord.Embed(
                title="💥 俄羅斯輪盤 — 對決結束",
                description=(
                    f"{loss_line}\n"
                    f"🏆 勝者 <@{win_uid}> 贏得 **`{self.pot:,}`** 東雲幣！"
                ),
                color=0xED4245,
            )
        else:
            emb = discord.Embed(title="💥 俄羅斯輪盤 — 結束", color=0xED4245)
    elif self.mode == "royale" and not self.lobby_started:
        n = len(self.participant_ids)
        bullets = royale_bullet_count(n)
        chambers = royale_chamber_count(n, bullets, self.default_chambers)
        emb = discord.Embed(
            title="👥 俄羅斯輪盤淘汰賽 — 報名中",
            description=(
                f"主持人 <@{self.host_id}>｜入場費 **`{self.bet_base:,}`** 東雲幣\n"
                f"目前 **`{n}` / {RR_ROYALE_MAX_PLAYERS}** 人"
                f"（至少 {RR_ROYALE_MIN_PLAYERS} 人開局）\n"
                f"開局配置：**`{chambers}`** 膛 **`{bullets}`** 彈"
                f"（子彈＝人數 − 1；{RR_ROYALE_BASE_PLAYERS} 人 {self.default_chambers} 膛，"
                f"每多 1 人 +{RR_ROYALE_CHAMBERS_PER_EXTRA_PLAYER} 膛）\n"
                f"參加者：{self.mention_line()}"
            ),
            color=0xFEE75C,
        )
    else:
        title = "👥 俄羅斯輪盤淘汰賽" if self.mode == "royale" else "🔫 俄羅斯輪盤對決"
        desc = f"彩池：**`{self.pot:,}`** 東雲幣\n"
        if self.mode == "duel":
            desc = (
                f"<@{self.participant_ids[0]}> vs <@{self.participant_ids[1]}>\n"
                f"彩池：**`{self.pot:,}`** 東雲幣"
            )
        elif self.mode == "royale":
            alive_line = " ".join(f"<@{uid}>" for uid in self.alive_ids)
            desc += f"倖存：**{alive_line}**"
        emb = discord.Embed(title=title, description=desc, color=0x5865F2)
        if self.turn_uid is not None:
            emb.add_field(name="輪到", value=f"<@{self.turn_uid}>", inline=True)
        remaining = self.remaining_chambers
        rem_bullets = self.remaining_bullets()
        emb.add_field(name="剩餘膛室", value=str(remaining), inline=True)
        if self.mode == "royale":
            emb.add_field(
                name="彈倉",
                value=f"`{len(self.bullet_positions)}` 彈 / `{self.chambers}` 膛",
                inline=True,
            )
        prob = hit_probability(remaining, rem_bullets)
        if remaining and rem_bullets:
            prob_text = f"`{prob:.1f}%`（{rem_bullets}/{remaining}）"
        elif remaining:
            prob_text = f"`0%`（0/{remaining}）"
        else:
            prob_text = "`0%`"
        emb.add_field(name="下一發中彈機率", value=prob_text, inline=True)
        if awaiting_choice:
            emb.set_footer(text="空膛！可繼續射、加注繼續，或停手換人")
    if self.history:
        emb.add_field(name="歷程", value="\n".join(self.history[-8:]), inline=False)
    return emb


def build_history_embed(self) -> discord.Embed:
    """完整歷程，給歷程按鈕以私人訊息回覆用。"""
    emb = discord.Embed(
        title="📜 本場俄羅斯輪盤歷程",
        description=(
            f"模式：**{'淘汰賽' if self.mode == 'royale' else '1v1 對決'}**"
            f"｜彩池：**`{self.pot:,}`** 東雲幣\n"
            f"參加者：{self.mention_line()}"
        ),
        color=0x5865F2,
    )
    if not self.history:
        emb.add_field(name="歷程", value="（這場還沒有任何動作）", inline=False)
        return emb
    # 單一 field 上限 1024 字，超過就拆成多個
    chunks: typing.List[str] = []
    buf = ""
    for idx, text in enumerate(self.history, start=1):
        line = f"`{idx:>2}.` {text}"
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > 1024:
            chunks.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    truncated = len(chunks) > 5
    if truncated:
        chunks = chunks[-5:]
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        emb.add_field(
            name="歷程" if total == 1 else f"歷程（{i}/{total}）",
            value=chunk,
            inline=False,
        )
    if truncated:
        emb.set_footer(text="歷程過長，僅顯示最近的部分")
    return emb


