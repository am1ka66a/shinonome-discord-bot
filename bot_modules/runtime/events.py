import asyncio
import datetime
import random
import time
import typing

import discord


def register_events(bot, ctx: typing.Dict[str, typing.Any]) -> typing.Dict[str, typing.Callable[..., typing.Any]]:
    logger = ctx["logger"]
    now_tw_naive = ctx["now_tw_naive"]
    db_to_thread = ctx["db_to_thread"]
    award_vc_rewards_sync = ctx["award_vc_rewards_sync"]
    purge_old_logs_sync = ctx["purge_old_logs_sync"]
    relay_dm_to_staff_channel = ctx["relay_dm_to_staff_channel"]
    relay_staff_reply_to_dm_user = ctx["relay_staff_reply_to_dm_user"]
    relay_user_message_to_staff_channel = ctx["relay_user_message_to_staff_channel"]
    process_on_message_activity_sync = ctx["process_on_message_activity_sync"]
    process_level_ups = ctx["process_level_ups"]
    cleanup_local_caches = ctx.get("cleanup_local_caches")
    finalize_due_lottery_rounds_async = ctx.get("finalize_due_lottery_rounds_async")
    lottery_draw_check_seconds = float(ctx.get("LOTTERY_DRAW_CHECK_SECONDS", 3600))

    dm_relay_channel_id = int(ctx["DM_RELAY_CHANNEL_ID"])
    delete_log_channel_id = int(ctx["DELETE_LOG_CHANNEL_ID"])
    msg_db_flush_every_seconds = float(ctx["MSG_DB_FLUSH_EVERY_SECONDS"])
    msg_db_flush_count = int(ctx["MSG_DB_FLUSH_COUNT"])
    exp_cooldown_seconds = float(ctx["EXP_COOLDOWN_SECONDS"])
    chat_exp_multiplier = int(ctx["CHAT_EXP_MULTIPLIER"])
    log_retention_days = int(ctx["LOG_RETENTION_DAYS"])
    log_purge_interval_seconds = int(ctx["LOG_PURGE_INTERVAL_SECONDS"])
    level_mile_tiers = tuple(ctx["LEVEL_MILE_TIERS"])

    pending_msg_counts = ctx["_pending_msg_counts"]
    last_msg_flush_ts = ctx["_last_msg_flush_ts"]
    last_exp_award_ts = ctx["_last_exp_award_ts"]

    delete_log_embed_color = 0x5865F2
    delete_log_max_embeds_per_message = 10
    image_attachment_suffix = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")
    video_attachment_suffix = (".mp4", ".webm", ".mov", ".mkv")

    async def logs_retention_task():
        await bot.wait_until_ready()
        while not bot.is_closed():
            try:
                if log_retention_days <= 0:
                    await asyncio.sleep(log_purge_interval_seconds)
                    continue
                removed = await db_to_thread(purge_old_logs_sync, log_retention_days)
                if removed:
                    logger.info(
                        "logs 保留最近 %s 天：已刪除 %s 筆過期紀錄",
                        log_retention_days,
                        removed,
                    )
            except Exception as e:
                logger.exception("logs 定期清理失敗: %s", e)
            await asyncio.sleep(log_purge_interval_seconds)

    async def vc_reward_task():
        await bot.wait_until_ready()
        while not bot.is_closed():
            await asyncio.sleep(600)
            now = now_tw_naive()
            candidate_user_ids: typing.Set[str] = set()
            for guild in bot.guilds:
                for vc in guild.voice_channels:
                    for member in vc.members:
                        if member.bot or member.voice.self_deaf or member.voice.deaf:
                            continue
                        candidate_user_ids.add(str(member.id))
            try:
                await db_to_thread(award_vc_rewards_sync, list(candidate_user_ids), now)
            except Exception as e:
                logger.exception("語音獎勵發放失敗: %s", e)

    async def cache_cleanup_task():
        """定時清理 bot.py 的 TTL 快取鍵，防止字典長期膨脹。"""
        await bot.wait_until_ready()
        while not bot.is_closed():
            await asyncio.sleep(60)
            if cleanup_local_caches is None:
                continue
            try:
                await asyncio.to_thread(cleanup_local_caches)
            except Exception as e:
                logger.exception("快取清理失敗: %s", e)

    async def lottery_draw_task():
        await bot.wait_until_ready()
        while not bot.is_closed():
            await asyncio.sleep(lottery_draw_check_seconds)
            if finalize_due_lottery_rounds_async is None:
                continue
            try:
                results = await finalize_due_lottery_rounds_async()
                for item in results or []:
                    if item.get("winner_id"):
                        logger.info(
                            "日彩池開獎 %s：winner=%s pool=%s tickets=%s",
                            item.get("day_key"),
                            item.get("winner_id"),
                            item.get("pool"),
                            item.get("tickets"),
                        )
            except Exception as e:
                logger.exception("日彩池開獎檢查失敗: %s", e)

    def classify_attachment(a: discord.Attachment) -> str:
        ct = (getattr(a, "content_type", None) or "").lower()
        fn = (getattr(a, "filename", "") or "").lower()
        if ct.startswith("image/") or any(fn.endswith(s) for s in image_attachment_suffix):
            return "image"
        if ct.startswith("video/") or any(fn.endswith(s) for s in video_attachment_suffix):
            return "video"
        return "file"

    def delete_log_image_embed(url: str) -> discord.Embed:
        e = discord.Embed(color=delete_log_embed_color)
        e.set_image(url=url)
        return e

    def chunk_plain_url_lines(urls: typing.Sequence[str], limit: int = 1950) -> typing.List[str]:
        chunks: typing.List[str] = []
        buf: typing.List[str] = []
        size = 0
        for u in urls:
            add = len(u) + (1 if buf else 0)
            if buf and size + add > limit:
                chunks.append("\n".join(buf))
                buf = [u]
                size = len(u)
            else:
                if buf:
                    size += 1
                buf.append(u)
                size += len(u)
        if buf:
            chunks.append("\n".join(buf))
        return chunks

    async def send_delete_log_image_overflow(ch: discord.TextChannel, urls: typing.Sequence[str]) -> None:
        if not urls:
            return
        for i in range(0, len(urls), delete_log_max_embeds_per_message):
            part = urls[i : i + delete_log_max_embeds_per_message]
            await ch.send(embeds=[delete_log_image_embed(u) for u in part])

    async def is_message_deleted_by_bot(guild: discord.Guild, message: discord.Message) -> bool:
        if guild is None:
            return False
        try:
            me = guild.me
            if me and not me.guild_permissions.view_audit_log:
                return False
        except Exception:
            return False
        try:
            async for entry in guild.audit_logs(limit=8, action=discord.AuditLogAction.message_delete):
                target = entry.target
                if not isinstance(target, (discord.Member, discord.User)):
                    continue
                if message.author and target.id != message.author.id:
                    continue
                if entry.extra and getattr(entry.extra, "channel", None):
                    if entry.extra.channel.id != message.channel.id:
                        continue
                try:
                    age = (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds()
                    if age > 10:
                        continue
                except Exception:
                    pass
                actor = entry.user
                if actor is not None and getattr(actor, "bot", False):
                    return True
                return False
        except Exception:
            return False
        return False

    @bot.event
    async def on_message(message):
        if message.author.bot:
            return
        try:
            if message.guild is None:
                await relay_dm_to_staff_channel(message)
                return
            if message.channel.id == dm_relay_channel_id:
                if not message.webhook_id and await relay_staff_reply_to_dm_user(message):
                    return
            if (
                message.guild
                and message.channel.id != dm_relay_channel_id
                and bot.user is not None
                and bot.user in message.mentions
            ):
                await relay_user_message_to_staff_channel(message, is_dm=False)
            if not message.guild:
                return

            user_id = str(message.author.id)
            now = now_tw_naive()
            now_ts = time.time()

            pending_msg_counts[user_id] = pending_msg_counts.get(user_id, 0) + 1
            pending = pending_msg_counts[user_id]
            last_flush = last_msg_flush_ts.get(user_id, 0.0)
            should_flush = pending >= msg_db_flush_count or (now_ts - last_flush) >= msg_db_flush_every_seconds

            last_exp_ts = last_exp_award_ts.get(user_id, 0.0)
            exp_due = (now_ts - last_exp_ts) >= exp_cooldown_seconds
            if not should_flush and not exp_due:
                return

            exp_gain = random.randint(12, 20) * chat_exp_multiplier if exp_due else 0
            result = await db_to_thread(
                process_on_message_activity_sync,
                user_id,
                pending,
                now,
                exp_due,
                exp_gain,
            )
            pending_msg_counts[user_id] = 0
            last_msg_flush_ts[user_id] = now_ts
            if result.get("exp_awarded"):
                last_exp_award_ts[user_id] = now_ts
                o = int(result.get("old_level") or 1)
                n = int(result.get("new_level") or 1)
                if n > o and any(o < m <= n for m in level_mile_tiers):
                    asyncio.create_task(
                        process_level_ups(message.author, o, n, guild_id=message.guild.id)
                    )
        except Exception as e:
            logger.exception("on_message 錯誤: %s", e)
        finally:
            await bot.process_commands(message)

    @bot.event
    async def on_message_delete(message: discord.Message):
        try:
            if delete_log_channel_id <= 0:
                return
            if message.guild is None:
                return
            if await is_message_deleted_by_bot(message.guild, message):
                return
            author = message.author
            if author is not None and author.bot:
                return
            log_ch = bot.get_channel(delete_log_channel_id)
            if log_ch is None:
                try:
                    log_ch = await bot.fetch_channel(delete_log_channel_id)
                except Exception:
                    return
            if not isinstance(log_ch, discord.TextChannel):
                return

            content = (message.content or "").strip() or "*（無文字內容）*"
            guild = message.guild
            icon = None
            try:
                if guild.icon:
                    icon = str(guild.icon.url)
            except Exception:
                icon = None

            author_line = (
                f"{author.mention} · `{author.id}`"
                if author is not None
                else "發送者：`（訊息未在快取中，無法還原發送者）`"
            )
            try:
                ch_label = message.channel.mention
            except Exception:
                ch_label = f"<#{getattr(message.channel, 'id', 0)}>"

            desc_lines = [author_line, f"{ch_label} · {guild.name}"]
            if message.created_at:
                desc_lines.append(f"原訊息時間：<t:{int(message.created_at.timestamp())}:F>")

            emb = discord.Embed(
                title="訊息已刪除",
                description="\n".join(desc_lines),
                color=delete_log_embed_color,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            if icon and icon.startswith("http"):
                emb.set_author(name="刪除紀錄", icon_url=icon)
            else:
                emb.set_author(name="刪除紀錄")
            emb.add_field(name="原文", value=content[:1024], inline=False)

            embeds_out: typing.List[discord.Embed] = [emb]
            overflow_image_urls: typing.List[str] = []
            images: typing.List[typing.Tuple[discord.Attachment, str]] = []
            videos: typing.List[typing.Tuple[discord.Attachment, str]] = []
            other_files: typing.List[typing.Tuple[discord.Attachment, str]] = []

            if message.attachments:
                for a in message.attachments:
                    try:
                        url = a.url
                    except Exception:
                        continue
                    if not url:
                        continue
                    kind = classify_attachment(a)
                    if kind == "image":
                        images.append((a, url))
                    elif kind == "video":
                        videos.append((a, url))
                    else:
                        other_files.append((a, url))

                if images:
                    urls_only = [u for _, u in images]
                    emb.set_image(url=urls_only[0])
                    idx = 1
                    while idx < len(urls_only) and len(embeds_out) < delete_log_max_embeds_per_message:
                        embeds_out.append(delete_log_image_embed(urls_only[idx]))
                        idx += 1
                    overflow_image_urls = list(urls_only[idx:])

                if other_files:
                    link_lines = []
                    for i, (a, url) in enumerate(other_files, start=1):
                        name = getattr(a, "filename", None) or f"附件{i}"
                        link_lines.append(f"[`{name}`]({url})")
                    emb.add_field(name="附件（檔案）", value="\n".join(link_lines)[:1024], inline=False)

            emb.set_footer(text=f"訊息 ID · {message.id}")
            video_urls = [u for _, u in videos]
            video_chunks = chunk_plain_url_lines(video_urls) if video_urls else []

            send_kw: typing.Dict[str, typing.Any] = {"embeds": embeds_out}
            if video_chunks:
                send_kw["content"] = video_chunks[0]
            await log_ch.send(**send_kw)
            for vc in video_chunks[1:]:
                await log_ch.send(content=vc)
            if overflow_image_urls:
                await send_delete_log_image_overflow(log_ch, overflow_image_urls)
        except Exception as e:
            logger.exception("on_message_delete 錯誤: %s", e)

    return {
        "logs_retention_task": logs_retention_task,
        "vc_reward_task": vc_reward_task,
        "cache_cleanup_task": cache_cleanup_task,
        "lottery_draw_task": lottery_draw_task,
    }
