import re
import typing

import discord
from discord.ext import commands

from bot_modules import config

DISCORD_MESSAGE_CAP = config.DISCORD_MESSAGE_CAP


def split_discord_message_chunks(
    text: str, limit: int = DISCORD_MESSAGE_CAP
) -> typing.List[str]:
    """依字元長度切分，供多則訊息送出（一般文字／DM）。"""
    if not text:
        return []
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def chunk_text_lines(lines: typing.List[str], max_len: int = 1900) -> typing.List[str]:
    chunks: typing.List[str] = []
    buf: typing.List[str] = []
    size = 0
    for line in lines:
        add = len(line) + (1 if buf else 0)
        if buf and size + add > max_len:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            if buf:
                size += 1
            buf.append(line)
            size += len(line)
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def parse_discord_user_id(raw: typing.Optional[str]) -> typing.Optional[int]:
    """解析純數字 ID 或 <@...> 提及。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = re.fullmatch(r"<@!?(\d{17,20})>", s)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d{17,20})", s)
    if m:
        return int(m.group(1))
    return None


async def resolve_slash_target(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member],
    user_id: typing.Optional[str],
    *,
    required: bool = True,
    in_guild_only: bool = False,
) -> typing.Tuple[typing.Optional[typing.Union[discord.Member, discord.User]], typing.Optional[str]]:
    """
    優先使用選取的 member；否則解析 user_id。
    in_guild_only=True 時必須為本伺服器成員（搶劫／轉帳等）；False 時若不在伺服器則改以 fetch_user（後台／黑名單等）。
    """
    if member is not None:
        return member, None
    uid = parse_discord_user_id(user_id)
    if uid is None:
        if not required:
            return None, None
        return None, "請選擇成員，或在「使用者 ID」填寫 17～19 位數字（亦可貼 `<@...>` 提及）。"
    guild = interaction.guild
    client = interaction.client
    if guild is not None:
        cached = guild.get_member(uid)
        if cached is not None:
            return cached, None
        try:
            m = await guild.fetch_member(uid)
            return m, None
        except discord.NotFound:
            if in_guild_only:
                return None, "找不到此成員（請確認對方仍在這個伺服器）。"
            try:
                u = await client.fetch_user(uid)
                return u, None
            except discord.NotFound:
                return None, "找不到此 Discord 使用者。"
            except discord.HTTPException as e:
                return None, f"無法查詢使用者：{e}"
        except discord.HTTPException as e:
            return None, f"無法查詢成員：{e}"
    if in_guild_only:
        return None, "請在伺服器頻道使用此指令。"
    try:
        u = await client.fetch_user(uid)
        return u, None
    except discord.NotFound:
        return None, "找不到此 Discord 使用者。"
    except discord.HTTPException as e:
        return None, f"無法查詢使用者：{e}"


async def interaction_defer_if_needed(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = False,
    thinking: bool = True,
) -> None:
    """先 ACK slash interaction，避免同步 DB 或外部 API 慢時觸發 3 秒逾時。"""
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
    except discord.InteractionResponded:
        pass


async def interaction_send(
    interaction: discord.Interaction,
    *args,
    **kwargs,
):
    """已 defer 的互動走 followup；未回應的互動走原本 response。"""
    if interaction.response.is_done():
        kwargs.setdefault("wait", True)
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)


def make_host_check(allowed_host_ids: typing.Sequence[int]):
    def predicate(ctx):
        return ctx.author.id in allowed_host_ids

    return commands.check(predicate)
