import asyncio
import datetime
import logging
import random
import re
import typing

import discord
from discord import app_commands

from bot_modules.db import db_cursor
from bot_modules.discord_helpers import interaction_defer_if_needed, interaction_send


def parse_say_reply_target(value: str) -> typing.Tuple[typing.Optional[int], typing.Optional[int], int]:
    """Accept a message ID or a server message link; never fetch arbitrary URLs."""
    raw = value.strip().removeprefix("<").removesuffix(">")
    if re.fullmatch(r"[0-9]{15,20}", raw):
        return None, None, int(raw)
    match = re.fullmatch(
        r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/"
        r"([0-9]{15,20})/([0-9]{15,20})/([0-9]{15,20})(?:[?#][^\s]*)?",
        raw,
    )
    if not match:
        raise ValueError("請貼上 Discord 訊息連結，或指定頻道內的訊息 ID。")
    return tuple(int(part) for part in match.groups())


def register_fun_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    ALLOWED_HOST_IDS = ctx["ALLOWED_HOST_IDS"]
    logger = logging.getLogger(__name__)

    ROB_BASE_SUCCESS_RATE = ctx["ROB_BASE_SUCCESS_RATE"]
    COP_HUNT_CAPTURE_BASE_PCT = ctx["COP_HUNT_CAPTURE_BASE_PCT"]
    COP_HUNT_CAPTURE_PER_STAR_PCT = ctx["COP_HUNT_CAPTURE_PER_STAR_PCT"]
    COP_HUNT_FEE = ctx["COP_HUNT_FEE"]
    WANTED_BUYOUT_COST = ctx["WANTED_BUYOUT_COST"]
    GOOD_CITIZEN_CERT_COST = ctx["GOOD_CITIZEN_CERT_COST"]
    GOOD_CITIZEN_DESTROY_COST = ctx["GOOD_CITIZEN_DESTROY_COST"]
    BAIL_COST = ctx["BAIL_COST"]
    COUNTER_ROB_BASE_SUCCESS_RATE = ctx["COUNTER_ROB_BASE_SUCCESS_RATE"]
    MINECRAFT_DEATH_MESSAGES = ctx["MINECRAFT_DEATH_MESSAGES"]
    MINECRAFT_ITEMS = ctx["MINECRAFT_ITEMS"]
    BICYCLE_COOLDOWN_SECONDS = ctx["BICYCLE_COOLDOWN_SECONDS"]
    TW_TZ = ctx["TW_TZ"]
    resolve_slash_target = ctx["resolve_slash_target"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    now_tw_naive = ctx["now_tw_naive"]
    get_db_connection = ctx["get_db_connection"]
    lock_user_rows = ctx["lock_user_rows"]
    log_transaction_in_tx = ctx["log_transaction_in_tx"]

    def _kill_message_label(template: str) -> str:
        label = template.replace("{target} ", "").replace("{target}", "").strip()
        return label[:100]

    def _build_kill_message(
        template: str,
        target_member: discord.Member,
        killer: discord.abc.User,
        item: str,
    ) -> str:
        target_text = target_member.mention
        return (
            template.format(target=target_text)
            .replace("<死者>", target_text)
            .replace("死者", target_text)
            .replace("擊殺者", killer.mention)
            .replace("击杀者", killer.mention)
            .replace("物品", item)
        )

    def _resolve_kill_template(death: typing.Optional[str]) -> str:
        if not death:
            return random.choice(MINECRAFT_DEATH_MESSAGES)
        if death.isdigit():
            idx = int(death)
            if 0 <= idx < len(MINECRAFT_DEATH_MESSAGES):
                return MINECRAFT_DEATH_MESSAGES[idx]
        for template in MINECRAFT_DEATH_MESSAGES:
            if death == template or death == _kill_message_label(template):
                return template
        death_lower = death.lower()
        for template in MINECRAFT_DEATH_MESSAGES:
            if death_lower in _kill_message_label(template).lower():
                return template
        if "{target}" not in death and "死者" not in death:
            return f"{{target}} {death}"
        return death

    def _resolve_kill_item(item: typing.Optional[str]) -> str:
        if not item:
            return random.choice(MINECRAFT_ITEMS)
        if item.isdigit():
            idx = int(item)
            if 0 <= idx < len(MINECRAFT_ITEMS):
                return MINECRAFT_ITEMS[idx]
        for known in MINECRAFT_ITEMS:
            if item == known:
                return known
        item_lower = item.lower()
        for known in MINECRAFT_ITEMS:
            if item_lower in known.lower():
                return known
        return item.strip()

    @bot.tree.command(name="help", description="機器人指令總覽（一般玩家）")
    async def help_slash(interaction: discord.Interaction):
        """東雲幣、賭場、通緝／警察、等級等 Slash 說明（不含主機／管理員專用指令）。"""
        emb = discord.Embed(
            title="📖 東雲機器人指令說明",
            description="以下為**一般玩家**常用指令；管理／主機專用請見伺服公告或管理員。",
            color=0x5865F2,
        )
        _cop_hunt_pct_1star = min(
            95,
            COP_HUNT_CAPTURE_BASE_PCT + COP_HUNT_CAPTURE_PER_STAR_PCT,
        )
        emb.add_field(
            name="💰 日常與經濟",
            value=(
                "`/daily` — 每日簽到領幣\n"
                "`/hourly` — 每小時簽到（依等級累積）\n"
                "`/beg` — 乞討\n"
                f"`/rob` — 搶劫（**僅搶匪**；約 **{int(round(ROB_BASE_SUCCESS_RATE * 100))}%** 基礎成功率、每級差 ±1%；**30 分鐘**冷卻；成功累積通緝）\n"
                "`/rescue` — 破產救濟（餘額 0 時）\n"
                "`/transfer` — 轉帳給其他玩家\n"
                "`/redpacket` — 發紅包\n"
                "`/record` — 最近帳務紀錄（翻頁）\n"
                "`/balance` — 餘額與戰績\n"
                "`/cooldowns` — 查看各指令冷卻\n"
                "`/profile` — 玩家名片\n"
                "`/rank` — 我的全站名次"
            ),
            inline=False,
        )
        emb.add_field(
            name="🃏 賭場與等級",
            value=(
                "`/bj` — 二十一點\n"
                "`/coinflip` — 拋硬幣（50% 勝率）\n"
                "`/russian_roulette duel` — 1v1 俄羅斯輪盤（可加注繼續）\n"
                "`/russian_roulette royale` — 3～6 人淘汰賽（子彈＝人數 − 1，每多 1 人 +3 膛）\n"
                "`/russian_roulette stats me` — 個人戰績\n"
                "`/russian_roulette stats wins` — 勝場榜\n"
                "`/russian_roulette stats rate` — 勝率榜\n"
                "`/lottery status` — 日彩池狀態\n"
                "`/lottery buy` — 購買日彩券\n"
                "`/duel` — E 卡決鬥（兩大局；第二大局交換陣營；奴贏王 +3、其餘決勝 +1；依積分分配彩池）\n"
                "`/level` — 等級與 EXP\n"
                "`/leaderboard` — 餘額榜前 10\n"
                "`/lvleaderboard` — 等級榜前 10\n"
                "`/casino_stats` — 經濟總金流統計"
            ),
            inline=False,
        )
        emb.add_field(
            name="🚔 通緝與警察",
            value=(
                "`/role_choose` — 選警察／搶匪／平民\n"
                "`/wanted_status` — 自己的通緝、監獄、搶劫紀錄\n"
                "`/wanted_list` — 目前通緝名單與可否追捕\n"
                f"`/good_citizen` — [平民] 付 `{GOOD_CITIZEN_CERT_COST:,}` 啟用防搶；再付同額解除（啟用/解除皆 24h 冷卻）\n"
                "`/good_citizen_list` — 查看目前良民證持有者\n"
                f"`/break_citizen` — 摧毀目標良民證（花費 `{GOOD_CITIZEN_DESTROY_COST:,}`，目標 10 天禁用）\n"
                f"`/cop_hunt` — 警察追捕（僅警察；每次 **`{COP_HUNT_FEE:,}`** 幣、成敗皆扣）。"
                f"成功率 **1★ 約 {_cop_hunt_pct_1star}%** 起，通緝每多 **1** 星 **+{COP_HUNT_CAPTURE_PER_STAR_PCT}%**，並受等級差影響（每級 ±1%，保底 **5%**、上限 **95%**）\n"
                f"`/wanted_buyout` — [搶匪] 付 `{WANTED_BUYOUT_COST:,}` 消除全部通緝星並**清空最近搶劫紀錄**（**24 小時**冷卻）\n"
                f"`/counter_rob` — 已改為平民被搶後**自動反制結算**（約 **{int(round(COUNTER_ROB_BASE_SUCCESS_RATE * 100))}%** 基礎、級差 ±1%）\n"
                f"`/bail` — 入獄繳 **基礎 `{BAIL_COST:,}` + 累計假釋欠款** 出獄"
            ),
            inline=False,
        )
        emb.add_field(
            name="🎮 其他",
            value="`/kill` — Minecraft 風格隨機死法（可選指定死亡方式與物品）\n"
            "`/bicycle` — 嘗試偷走奈音的腳踏車\n"
            "`/compare` — 比較兩位玩家\n"
            "`/8ball` — 魔法八號球\n"
            "`/choose` — 隨機選一項",
            inline=False,
        )
        emb.set_footer(text="私訊轉接、群組 @ 機器人可聯繫管理員｜管理員請用 /adminhelp（僅主機）")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @bot.tree.command(name="kill", description="在目前頻道送出 Minecraft 風格隨機死法")
    @app_commands.describe(
        target="目標（選人）",
        user_id="或填使用者 ID／貼提及",
        death="死亡方式（選填；可輸入或從建議選單挑選）",
        item="物品（選填；可輸入或從建議選單挑選）",
    )
    async def kill(
        interaction: discord.Interaction,
        target: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
        death: typing.Optional[str] = None,
        item: typing.Optional[str] = None,
    ):
        m_user, err = await resolve_slash_target(
            interaction, target, user_id, required=True, in_guild_only=True
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        if not isinstance(m_user, discord.Member):
            return await interaction.response.send_message("目標必須是此伺服器成員。", ephemeral=True)
        target = m_user
        template = _resolve_kill_template(death)
        resolved_item = _resolve_kill_item(item)
        msg = _build_kill_message(template, target, interaction.user, resolved_item)
        await interaction.response.send_message(msg)

    @kill.autocomplete("death")
    async def kill_death_autocomplete(
        interaction: discord.Interaction,
        current: typing.Optional[str],
    ):
        needle = (current or "").strip().lower()
        choices: typing.List[app_commands.Choice[str]] = []
        for idx, template in enumerate(MINECRAFT_DEATH_MESSAGES):
            label = _kill_message_label(template)
            if needle and needle not in label.lower() and needle not in template.lower():
                continue
            choices.append(app_commands.Choice(name=label, value=str(idx)))
            if len(choices) >= 25:
                break
        return choices

    @kill.autocomplete("item")
    async def kill_item_autocomplete(
        interaction: discord.Interaction,
        current: typing.Optional[str],
    ):
        needle = (current or "").strip().lower()
        choices: typing.List[app_commands.Choice[str]] = []
        for idx, known in enumerate(MINECRAFT_ITEMS):
            if needle and needle not in known.lower():
                continue
            choices.append(app_commands.Choice(name=known[:100], value=str(idx)))
            if len(choices) >= 25:
                break
        return choices

    @bot.tree.command(name="bicycle", description="嘗試偷走奈音的腳踏車")
    async def bicycle_slash(interaction: discord.Interaction):
        await interaction_defer_if_needed(interaction)
        thief_id = str(interaction.user.id)
        target_id = "1027248561177509919"
        steal_amount = 100
        now = now_tw_naive()
        await ensure_user_exists_async(interaction.user.id, 50000)
        await ensure_user_exists_async(int(target_id), 0)

        # DB 工作全部在 with 內完成，回覆 Discord 的 await 一律等連線歸還後才做
        cooldown_ts: typing.Optional[int] = None
        outcome = ""
        def db_work(cooldown_ts=cooldown_ts, outcome=outcome):
            with db_cursor(commit=True, connect=get_db_connection) as c:
                lock_user_rows(c, [thief_id, target_id])
                c.execute("SELECT last_bicycle FROM users WHERE user_id=%s", (thief_id,))
                row = c.fetchone()
                last_bicycle = row[0] if row else None
                if last_bicycle and (now - last_bicycle).total_seconds() < BICYCLE_COOLDOWN_SECONDS:
                    remain = BICYCLE_COOLDOWN_SECONDS - int((now - last_bicycle).total_seconds())
                    cooldown_ts = int((now + datetime.timedelta(seconds=remain)).replace(tzinfo=TW_TZ).timestamp())
                elif random.random() >= 0.99:
                    c.execute("UPDATE users SET last_bicycle=%s WHERE user_id=%s", (now, thief_id))
                    outcome = "小黑龜再練練 連個腳踏車都偷不走"
                else:
                    c.execute(
                        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
                        (steal_amount, target_id, steal_amount),
                    )
                    if c.rowcount > 0:
                        c.execute(
                            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
                            (steal_amount, thief_id),
                        )
                        c.execute("UPDATE users SET last_bicycle=%s WHERE user_id=%s", (now, thief_id))
                        log_transaction_in_tx(c, target_id, -steal_amount, f"腳踏車被偷（偷車者:{thief_id}）")
                        log_transaction_in_tx(c, thief_id, steal_amount, f"偷走奈音腳踏車（目標:{target_id}）")
                        outcome = "你成功偷走了奈音的腳踏車!"
                    else:
                        c.execute("UPDATE users SET last_bicycle=%s WHERE user_id=%s", (now, thief_id))
                        outcome = "奈音身上沒錢了! 沒有腳踏車能偷!"
            return (cooldown_ts, outcome,)
        cooldown_ts, outcome, = await asyncio.to_thread(db_work)

        if cooldown_ts is not None:
            return await interaction_send(interaction,
                f"⏳ /bicycle 冷卻中，請於 <t:{cooldown_ts}:R> 再試。",
                ephemeral=True,
            )
        return await interaction_send(interaction, outcome)

    @bot.tree.command(name="say", description="[管理員] 讓機器人發送文字、圖片，或回覆指定訊息")
    @app_commands.describe(
        text="要發送的文字（可留空，只發圖片）",
        channel="目標文字頻道／討論串（未填時使用訊息連結的頻道，或目前頻道）",
        image="要上傳的圖片（可與文字一起發送）",
        reply_to="要回覆的訊息連結，或目標頻道內的訊息 ID（選填）",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def say_slash(
        interaction: discord.Interaction,
        text: typing.Optional[str] = None,
        channel: typing.Optional[typing.Union[discord.TextChannel, discord.Thread]] = None,
        image: typing.Optional[discord.Attachment] = None,
        reply_to: typing.Optional[str] = None,
    ):
        await send_say(interaction, text=text, channel=channel, image=image, reply_to=reply_to)

    async def send_say(
        interaction, *, text=None, channel=None, image=None, reply_to=None, request_id=None,
    ):
        if interaction.user.id not in ALLOWED_HOST_IDS:
            return await interaction_send(interaction, "❌ 你沒有權限使用此指令！", ephemeral=True)
        if interaction.guild is None:
            return await interaction_send(interaction, "請在伺服器頻道使用此指令。", ephemeral=True)
        await interaction_defer_if_needed(interaction, ephemeral=True)
        content = text if text and text.strip() else None
        if content is None and image is None:
            return await interaction_send(interaction, "請至少填寫文字或上傳一張圖片。", ephemeral=True)
        if content is not None and len(content) > 2000:
            return await interaction_send(interaction, "文字長度不能超過 2,000 字。", ephemeral=True)
        if image is not None:
            mime = (image.content_type or "").lower()
            extension = image.filename.rsplit(".", 1)[-1].lower()
            if not mime.startswith("image/") and not (
                not mime and extension in {"png", "jpg", "jpeg", "gif", "webp", "avif"}
            ):
                return await interaction_send(interaction, "image 欄位只接受圖片檔案。", ephemeral=True)
            if image.size > interaction.guild.filesize_limit:
                return await interaction_send(interaction, "圖片超過這個伺服器的附件大小上限。", ephemeral=True)

        target_channel = channel or interaction.channel
        message_id = None
        linked_channel_id = None
        if reply_to is not None:
            try:
                guild_id, linked_channel_id, message_id = parse_say_reply_target(reply_to)
            except ValueError as exc:
                return await interaction_send(interaction, str(exc), ephemeral=True)
            if guild_id is not None and guild_id != interaction.guild.id:
                return await interaction_send(interaction, "請使用目前伺服器內的訊息連結。", ephemeral=True)
            if channel is not None and linked_channel_id is not None and channel.id != linked_channel_id:
                return await interaction_send(interaction, "訊息連結與指定頻道不同，請確認 channel 或移除它。", ephemeral=True)

        upload = None
        audit = getattr(bot, "message_audit", None)
        request_id = request_id or f"say:{interaction.id}"
        audit_saved = False

        async def record_rejection():
            if audit_saved:
                try:
                    await asyncio.to_thread(audit, request_id, interaction.user.id, target_channel.id, message_id, "rejected")
                except Exception:
                    logger.exception("發話拒絕紀錄更新失敗 request=%s", request_id)
        try:
            if linked_channel_id is not None and channel is None:
                target_channel = bot.get_channel(linked_channel_id)
                if target_channel is None:
                    target_channel = await bot.fetch_channel(linked_channel_id)
            if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
                return await interaction_send(interaction, "請指定文字頻道或討論串。", ephemeral=True)
            if target_channel.guild.id != interaction.guild.id:
                return await interaction_send(interaction, "只能在目前伺服器內發話。", ephemeral=True)

            kwargs: typing.Dict[str, typing.Any] = {}
            if message_id is not None:
                original = await target_channel.fetch_message(message_id)
                kwargs["reference"] = original.to_reference(fail_if_not_exists=True)
                kwargs["mention_author"] = False
            if image is not None:
                upload = await image.to_file(spoiler=image.is_spoiler())
                kwargs["file"] = upload
            if audit is not None:
                try:
                    await asyncio.to_thread(audit, request_id, interaction.user.id, target_channel.id, message_id, "sending")
                    audit_saved = True
                except Exception:
                    logger.exception("發話紀錄建立失敗 request=%s", request_id)
                    return await interaction_send(interaction, "操作紀錄暫時無法建立，尚未發送；請稍後再試。", ephemeral=True)
            sent = await target_channel.send(content, **kwargs)
        except discord.Forbidden:
            await record_rejection()
            return await interaction_send(
                interaction, "❌ 機器人沒有足夠權限，請確認目標頻道的查看、發送訊息、讀取歷史訊息及附加檔案權限。", ephemeral=True,
            )
        except discord.NotFound:
            await record_rejection()
            return await interaction_send(interaction, "❌ 找不到指定頻道、訊息或圖片，可能已刪除。", ephemeral=True)
        except discord.HTTPException as exc:
            if exc.status < 500:
                await record_rejection()
            return await interaction_send(interaction, "❌ Discord 未能完成發送；請先確認目標頻道是否已收到，再決定是否重試。", ephemeral=True)
        finally:
            if upload is not None:
                upload.close()
        action = "回覆" if message_id is not None else "發送"
        audit_warning = ""
        if audit is not None:
            try:
                await asyncio.to_thread(audit, request_id, interaction.user.id, target_channel.id, message_id, "sent", sent.id)
            except Exception:
                logger.exception("訊息已送出但紀錄更新失敗 request=%s message=%s", request_id, sent.id)
                audit_warning = "\n操作紀錄暫未更新，請保留此訊息連結。"
        await interaction_send(
            interaction, f"✅ 已{action}到 {target_channel.mention}！[查看訊息]({sent.jump_url}){audit_warning}", ephemeral=True,
        )
