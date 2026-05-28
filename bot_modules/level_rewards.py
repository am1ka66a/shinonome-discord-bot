import os
import typing

import discord

LEVEL_MILE_TIERS: typing.Tuple[int, ...] = (20, 40, 60, 80, 100)
LEVEL_MILESTONE_COINS: typing.Dict[int, int] = {
    20: 500_000,
    40: 1_000_000,
    60: 2_000_000,
    80: 8_000_000,
    100: 20_000_000,
}
LEVEL_MILESTONE_FLAVOR: typing.Dict[int, str] = {
    20: "恭喜你升上20等。請繼續努力，當個好賭狗。",
    40: "恭喜你從賭狗進化成了奈音的狗，到了這裡請當個好狗狗，多催更奈音的女裝。",
    60: "你好閒，水群水到了60等，請繼續浪費時間在DC上面，多多賭博有助身心健康。",
    80: "到了這一階，你時間真的很多，到了這個等級請買一張2330供奉給am1ka，撫慰他的辛勞。",
    100: "封頂。你這傻逼能滿等也是個奇蹟= =。",
}


def level_milestone_guild_id() -> typing.Optional[int]:
    raw = (os.getenv("LEVEL_MILESTONE_GUILD_ID", "") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def level_auto_role_id(milestone: int) -> typing.Optional[int]:
    raw = (os.getenv(f"LEVEL_ROLE_ID_{milestone}", "") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def process_level_ups(
    member: typing.Union[discord.Member, discord.User],
    old_lv: int,
    new_lv: int,
    *,
    try_claim_milestone,
) -> None:
    if new_lv <= old_lv or getattr(member, "bot", False):
        return
    crossed = [m for m in LEVEL_MILE_TIERS if old_lv < m <= new_lv]
    if not crossed:
        return
    intro = f"從 Lv.{old_lv} 升至 Lv.{new_lv}，**首次**通過本階檻。"
    embed = discord.Embed(title="🎉 等級階段解鎖", description=intro, color=0x57F287)
    reward_lines = []
    flavor_paras: typing.List[str] = []
    for m in crossed:
        coin_amt = LEVEL_MILESTONE_COINS.get(m, 0)
        got = try_claim_milestone(member.id, m, coin_amt)
        if got >= 0:
            fl = LEVEL_MILESTONE_FLAVOR.get(m)
            if fl:
                flavor_paras.append(f"**【Lv.{m}】** {fl}")
            if got > 0:
                reward_lines.append(f"🎁 Lv.{m}：+**{got:,}** 東雲幣")
        rid = level_auto_role_id(m)
        g_limit = level_milestone_guild_id()
        if (
            rid
            and isinstance(member, discord.Member)
            and member.guild
            and g_limit is not None
            and member.guild.id == g_limit
        ):
            role = member.guild.get_role(rid)
            if role:
                try:
                    await member.add_roles(role, reason=f"首次達到 Lv.{m} 解鎖（{member.guild.name}）")
                    reward_lines.append(f"🎭 已授予身分組 {role.name}")
                except discord.Forbidden:
                    reward_lines.append(
                        f"⚠️ 無法加上身分組「{role.name}」：請確認 Bot 有**管理身分組**，且 Bot 的**位階**高於該身分組。"
                    )
                except discord.HTTPException:
                    reward_lines.append("⚠️ 授予身分組時發生錯誤，稍後可請管理員手動補上。")
            else:
                reward_lines.append(f"⚠️ 找不到 Lv.{m} 對應身分組（ID: {rid}），請確認此 ID 屬於目前伺服器。")
    if not flavor_paras and not reward_lines:
        return
    if flavor_paras:
        embed.add_field(
            name="階段致詞",
            value="\n\n".join(flavor_paras)[:3800],
            inline=False,
        )
    if reward_lines:
        embed.add_field(name="本次獎勵", value="\n".join(reward_lines)[:1000], inline=False)
    try:
        await member.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass
