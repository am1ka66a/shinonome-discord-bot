import datetime
import io
import re
import typing

import discord

RelayForwardMeta = typing.Tuple[int, bool, int, int, int]
_relay_forward_meta: typing.Dict[int, RelayForwardMeta] = {}


def register_relay(bot, ctx: typing.Dict[str, typing.Any]) -> typing.Dict[str, typing.Any]:
    logger = ctx["logger"]
    DM_RELAY_CHANNEL_ID = ctx["DM_RELAY_CHANNEL_ID"]
    DM_RELAY_NOTIFY_USER_ID = ctx["DM_RELAY_NOTIFY_USER_ID"]
    DISCORD_MESSAGE_CAP = ctx["DISCORD_MESSAGE_CAP"]
    split_discord_message_chunks = ctx["split_discord_message_chunks"]

    async def _resolve_relay_from_staff_reply(
        channel: discord.abc.GuildChannel, message: discord.Message
    ) -> typing.Optional[RelayForwardMeta]:
        """沿回覆鏈找到機器人轉發訊息，回傳完整 relay meta。"""
        ref_id = message.reference.message_id if message.reference and message.reference.message_id else None
        seen: typing.Set[int] = set()
        while ref_id and ref_id not in seen:
            seen.add(ref_id)
            meta = _relay_forward_meta.get(ref_id)
            if meta is not None:
                return meta
            try:
                ref_msg = await channel.fetch_message(ref_id)
            except Exception:
                return None
            parsed = _relay_meta_from_forward_message(ref_msg)
            if parsed is not None:
                _relay_forward_meta[ref_id] = parsed
                return parsed
            if ref_msg.reference and ref_msg.reference.message_id:
                ref_id = ref_msg.reference.message_id
            else:
                return None
        return None

    def _relay_meta_from_forward_message(ref_msg: discord.Message) -> typing.Optional[RelayForwardMeta]:
        """從已發送到轉接頻道的 embed 反解析 relay meta（供重啟後快取遺失時使用）。"""
        if bot.user is None or ref_msg.author.id != bot.user.id:
            return None
        if not ref_msg.embeds:
            return None

        emb = ref_msg.embeds[0]
        title = (emb.title or "").strip()
        if not title:
            return None
        allow_private_reply = "私訊轉發" in title

        field_map = {str(f.name): str(f.value) for f in emb.fields}
        sender_text = field_map.get("發送者", "")
        if not sender_text:
            return None

        m_uid = re.search(r"ID\s*`(\d{15,20})`", sender_text)
        if not m_uid:
            m_uid = re.search(r"<@!?(\d{15,20})>", sender_text)
        if not m_uid:
            return None
        target_user_id = int(m_uid.group(1))

        guild_id = 0
        channel_id = 0
        message_id = 0
        if not allow_private_reply:
            loc_text = field_map.get("頻道／原訊", "")
            m_jump = re.search(r"/channels/(\d{15,20})/(\d{15,20})/(\d{15,20})", loc_text)
            if m_jump:
                guild_id = int(m_jump.group(1))
                channel_id = int(m_jump.group(2))
                message_id = int(m_jump.group(3))
            else:
                return None

        return (target_user_id, allow_private_reply, guild_id, channel_id, message_id)

    async def relay_user_message_to_staff_channel(message: discord.Message, *, is_dm: bool) -> None:
        """私訊或群組 @ 機器人 -> 轉發到管理頻道。"""
        ch = bot.get_channel(DM_RELAY_CHANNEL_ID)
        if ch is None:
            try:
                ch = await bot.fetch_channel(DM_RELAY_CHANNEL_ID)
            except Exception as e:
                logger.warning("找不到私訊轉接頻道 %s: %s", DM_RELAY_CHANNEL_ID, e)
                return
        if not isinstance(ch, discord.TextChannel):
            logger.warning("DM_RELAY_CHANNEL_ID 不是文字頻道")
            return
        author = message.author
        text = (message.content or "").strip()
        title = "📩 私訊轉發" if is_dm else "📣 群組 @ 機器人"
        emb = discord.Embed(title=title, color=0x5865F2, timestamp=datetime.datetime.now(datetime.timezone.utc))
        emb.set_author(name=str(author), icon_url=author.display_avatar.url)
        emb.add_field(name="發送者", value=f"<@{author.id}> ｜ ID `{author.id}`", inline=False)
        if not is_dm and message.guild:
            gname = discord.utils.escape_markdown(message.guild.name or "")
            emb.add_field(name="伺服器", value=f"{gname}（`{message.guild.id}`）", inline=False)
            emb.add_field(
                name="頻道／原訊",
                value=f"{message.channel.mention}\n[前往原訊息]({message.jump_url})",
                inline=False,
            )
        emb.description = text[:4096] if text else "（無文字內容）"
        att_urls = [a.url for a in message.attachments[:10]]
        if att_urls:
            emb.add_field(name="附件連結", value="\n".join(att_urls)[:1024], inline=False)
        sticker_names = [str(s.name) for s in message.stickers][:5]
        if sticker_names:
            emb.add_field(name="貼圖", value=", ".join(sticker_names)[:1024], inline=False)
        notify_id = DM_RELAY_NOTIFY_USER_ID
        sent = await ch.send(
            content=f"<@{notify_id}>",
            embed=emb,
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=notify_id)]),
        )
        if is_dm:
            _relay_forward_meta[sent.id] = (author.id, True, 0, 0, 0)
        elif message.guild:
            _relay_forward_meta[sent.id] = (
                author.id,
                False,
                message.guild.id,
                message.channel.id,
                message.id,
            )
        else:
            _relay_forward_meta[sent.id] = (author.id, False, 0, 0, 0)

    async def _post_staff_reply_to_guild_channel(
        target_user_id: int,
        _guild_id: int,
        channel_id: int,
        original_message_id: int,
        staff_msg: discord.Message,
    ) -> None:
        """在原伺服器頻道以機器人身分回覆使用者原訊息（不發私訊）。"""
        text = (staff_msg.content or "").strip()
        ch = bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await bot.fetch_channel(channel_id)
            except Exception as e:
                raise RuntimeError(f"無法取得頻道：{e}") from e
        if not isinstance(ch, discord.abc.Messageable):
            raise RuntimeError("目標不是可發言頻道")

        ref_msg: typing.Optional[discord.Message] = None
        try:
            ref_msg = await ch.fetch_message(original_message_id)
        except discord.NotFound:
            ref_msg = None

        files_first: typing.List[discord.File] = []
        for att in staff_msg.attachments:
            try:
                data = await att.read()
                files_first.append(discord.File(io.BytesIO(data), filename=att.filename or "attachment"))
            except Exception:
                continue

        if not text and not files_first:
            raise RuntimeError("沒有可發送的文字或附件")

        parts: typing.List[str] = split_discord_message_chunks(text) if text else [""]

        am = discord.AllowedMentions(users=[discord.Object(id=target_user_id)])
        first_chunk = True
        for idx, part in enumerate(parts):
            use_ref = ref_msg if (first_chunk and ref_msg is not None) else None
            fs = files_first if idx == 0 else []
            if part:
                if first_chunk and ref_msg is None:
                    prefix = f"<@{target_user_id}> "
                    room = max(0, DISCORD_MESSAGE_CAP - len(prefix))
                    body = prefix + part[:room]
                else:
                    body = part[:DISCORD_MESSAGE_CAP]
                kwargs: typing.Dict[str, typing.Any] = {"content": body, "allowed_mentions": am}
                if use_ref is not None:
                    kwargs["reference"] = use_ref
                if fs:
                    kwargs["files"] = fs
                await ch.send(**kwargs)
            elif idx == 0 and fs:
                kwargs = {"allowed_mentions": am}
                if ref_msg is not None:
                    kwargs["reference"] = ref_msg
                    kwargs["files"] = fs
                    await ch.send(**kwargs)
                else:
                    await ch.send(
                        content=f"<@{target_user_id}>（附件）",
                        files=fs,
                        allowed_mentions=am,
                    )
            first_chunk = False

    async def relay_dm_to_staff_channel(message: discord.Message) -> None:
        """使用者私訊機器人 -> 轉發到管理頻道。"""
        await relay_user_message_to_staff_channel(message, is_dm=True)

    async def relay_staff_reply_to_dm_user(message: discord.Message) -> bool:
        """管理員在轉接頻道回覆轉發：私訊轉發 -> DM 對方；群組 @ 轉發 -> 在原頻道以機器人代發回覆（不私訊）。"""
        if message.channel.id != DM_RELAY_CHANNEL_ID:
            return False
        resolved = await _resolve_relay_from_staff_reply(message.channel, message)
        if resolved is None:
            return False
        uid, allow_private_reply, og_gid, og_cid, og_mid = resolved
        text = (message.content or "").strip()
        if not text and not message.attachments:
            try:
                await message.add_reaction("❔")
            except Exception:
                pass
            return True

        if not allow_private_reply:
            if not og_cid or not og_mid:
                try:
                    await message.reply("❌ 找不到原訊息位置，無法代發到群組。", mention_author=False)
                except Exception:
                    pass
                return True
            try:
                await _post_staff_reply_to_guild_channel(uid, og_gid, og_cid, og_mid, message)
            except Exception as e:
                logger.exception("代發群組回覆失敗: %s", e)
                try:
                    await message.reply(f"❌ 無法在原頻道代發：{e}", mention_author=False)
                except Exception:
                    pass
                return True
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
            return True

        try:
            user = await bot.fetch_user(uid)
            for chunk in split_discord_message_chunks(text):
                await user.send(chunk)
            for att in message.attachments:
                try:
                    data = await att.read()
                    await user.send(file=discord.File(io.BytesIO(data), filename=att.filename or "file"))
                except Exception:
                    await user.send(att.url)
        except discord.Forbidden:
            try:
                await message.reply("❌ 無法私訊該使用者（可能已關閉與機器人的私訊）。", mention_author=False)
            except Exception:
                pass
        except Exception as e:
            logger.exception("轉發管理員回覆到私訊失敗: %s", e)
            try:
                await message.reply(f"❌ 發送私訊失敗：{e}", mention_author=False)
            except Exception:
                pass
        else:
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
        return True

    return {
        "relay_dm_to_staff_channel": relay_dm_to_staff_channel,
        "relay_staff_reply_to_dm_user": relay_staff_reply_to_dm_user,
        "relay_user_message_to_staff_channel": relay_user_message_to_staff_channel,
    }
