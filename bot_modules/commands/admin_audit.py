"""Private audit UI for the host allowlist. All callbacks only read data."""
import asyncio
import json
import math
import time
import typing

import discord
from discord import app_commands

from bot_modules import admin_audit_repo
from bot_modules.discord_helpers import interaction_defer_if_needed, interaction_send
from bot_modules.ui.views import LinePagerView


def safe_text(value, limit=140):
    return discord.utils.escape_markdown(str(value or "").replace("\n", " "))[:limit]


class AuditPager(LinePagerView):
    def __init__(self, owner_id, host_ids, *args, **kwargs):
        self.host_ids = host_ids
        super().__init__(owner_id, *args, **kwargs)

    async def interaction_check(self, interaction):
        if interaction.user.id not in self.host_ids:
            await interaction_send(interaction, "你沒有查帳權限。", ephemeral=True)
            return False
        return await super().interaction_check(interaction)


class AccountLedgerView(discord.ui.View):
    def __init__(self, owner_id, host_ids, uid, snapshot, connect, logger):
        super().__init__(timeout=180)
        self.owner_id, self.host_ids, self.uid = owner_id, host_ids, str(uid)
        self.snapshot, self.connect, self.logger = snapshot, connect, logger
        self.page = snapshot["page"]
        self.cursors, self.index = [None], 0
        self.guard = asyncio.Lock()
        self.message = None
        self.refresh()

    def refresh(self):
        self.previous.disabled = self.index == 0
        self.next_page.disabled = not self.page.has_more

    def build_embed(self):
        balance, exp, level, games, wins, profit = self.snapshot["account"]
        embed = discord.Embed(title="管理員查帳", color=0x5865F2)
        embed.description = (
            f"玩家 <@{self.uid}>（`{self.uid}`）\n"
            f"查詢時間：<t:{self.snapshot['queried_at']}:F>\n"
            f"餘額 **{int(balance):,}**｜Lv.{int(level)}｜EXP {int(exp):,}\n"
            f"遊戲 {int(games):,} 局｜勝場 {int(wins):,}｜總盈虧 {int(profit):+,}\n"
            f"黑名單：{'是' if self.snapshot['blacklisted'] else '否'}\n\n"
        )
        lines = []
        for ledger_id, amount, reason, created, source_id in self.page.rows:
            short_id = f"｜短期流水 #{source_id}" if source_id is not None else ""
            lines.append(f"總帳 **#{ledger_id}**{short_id}｜{int(amount):+,}\n"
                         f"{safe_text(created, 20)} · {safe_text(reason)}")
        embed.description += "\n\n".join(lines) or "目前沒有總帳流水。"
        embed.set_footer(text=f"第 {self.index+1} 頁｜固定查詢快照；重新執行指令可更新｜總帳 ID 與短期流水 ID 不同")
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or interaction.user.id not in self.host_ids:
            await interaction_send(interaction, "只有原查詢管理者可以操作此面板。", ephemeral=True)
            return False
        return True

    async def move(self, interaction, direction):
        if not await self.interaction_check(interaction):
            return
        await interaction_defer_if_needed(interaction, ephemeral=True, thinking=False)
        async with self.guard:
            target = self.index + direction
            if target < 0 or (direction > 0 and not self.page.has_more):
                return
            before = self.page.rows[-1][0] if target == len(self.cursors) else self.cursors[target]
            try:
                page = await asyncio.to_thread(admin_audit_repo.ledger_page, self.uid,
                    self.page.upper_id, before, self.connect)
            except Exception:
                self.logger.exception("查帳翻頁失敗 user=%s", self.uid)
                return await interaction_send(interaction, "暫時無法取得流水，頁面保持不變，請稍後重試。", ephemeral=True)
            if target == len(self.cursors):
                self.cursors.append(before)
            self.index, self.page = target, page
            self.refresh()
            await interaction.edit_original_response(embed=self.build_embed(), view=self,
                allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="較新流水", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self.move(interaction, -1)

    @discord.ui.button(label="較舊流水", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        await self.move(interaction, 1)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class BaselineConfirmView(discord.ui.View):
    """Second step before recording the reconciliation baseline."""
    def __init__(self, owner_id, host_ids, connect, logger):
        super().__init__(timeout=60)
        self.owner_id, self.host_ids, self.connect, self.logger = owner_id, host_ids, connect, logger
        self.message = None
        self.done = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or interaction.user.id not in self.host_ids:
            await interaction_send(interaction, "只有原查詢管理者可以操作此面板。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="確認：以目前差額設為基準", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if self.done:
            return
        self.done = True
        self.stop()
        await interaction_defer_if_needed(interaction, ephemeral=True, thinking=False)
        try:
            result = await asyncio.to_thread(admin_audit_repo.set_reconcile_baseline, interaction.user.id, self.connect)
        except Exception:
            self.logger.exception("對帳基準寫入失敗 actor=%s", interaction.user.id)
            return await interaction_send(interaction, "基準寫入失敗，沒有變更任何資料，請稍後重試。", ephemeral=True)
        self.logger.warning("對帳基準已更新 actor=%s accounts=%s total=%s",
                            interaction.user.id, result["accounts"], result["total_offset"])
        await interaction.edit_original_response(
            content=f"✅ 已記錄基準：{result['accounts']:,} 個帳戶，總差額 {result['total_offset']:+,}。"
                    "之後的對帳只會顯示基準之後新出現的差額。", embed=None, view=None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="已取消，沒有變更任何資料。", embed=None, view=None)

    async def on_timeout(self):
        if self.message is not None and not self.done:
            try:
                await self.message.edit(content="確認逾時，沒有變更任何資料。", embed=None, view=None)
            except discord.HTTPException:
                pass


def reconcile_lines(report):
    lines = []
    for uid, balance, ledger_sum, offset, drift in report.drifted:
        base = f"｜基準 {offset:+,}" if offset else ""
        lines.append(f"<@{uid}>（`{uid}`）差額 **{drift:+,}**\n"
                     f"餘額 {balance:,}｜總帳合計 {ledger_sum:,}{base}")
    return lines


def reconcile_summary(report):
    if report.baseline:
        actor, created, accounts, total = report.baseline
        baseline = f"基準：{safe_text(created, 20)} 由 `{actor}` 設定（{int(accounts):,} 帳戶，{int(total):+,}）"
    else:
        baseline = "基準：尚未設定（差額包含總帳建立前的歷史）"
    orphan = ""
    if report.orphan_users:
        orphan = f"\n⚠️ 總帳中有 {report.orphan_users} 個不存在的帳戶，合計 {report.orphan_amount:+,}"
    status = "✅ 全部相符" if not report.drifted else f"⚠️ {report.accounts - report.matched:,} 個帳戶不符"
    return (f"{status}\n檢查 {report.accounts:,} 個帳戶｜相符 {report.matched:,}｜差額合計 {report.total_drift:+,}\n"
            f"{baseline}{orphan}\n檢查時間：<t:{report.checked_at}:F>")


def register_admin_audit_commands(bot, ctx):
    hosts = ctx["ALLOWED_HOST_IDS"]
    connect = ctx["get_db_connection"]
    resolve = ctx["resolve_slash_target"]
    logger = ctx["logger"]
    group = app_commands.Group(name="admin_audit", description="[管理員] 私人查帳與結算狀態")
    group.default_permissions = discord.Permissions(administrator=True)

    @group.error
    async def audit_error(interaction, error):
        logger.error("管理員查帳失敗: %s", error, exc_info=(type(error), error, error.__traceback__))
        await interaction_send(interaction, "查帳暫時無法完成，請稍後重試；沒有變更任何資料。", ephemeral=True)

    async def authorize(interaction):
        if interaction.user.id not in hosts:
            await interaction_send(interaction, "你沒有查帳權限。", ephemeral=True)
            return False
        await interaction_defer_if_needed(interaction, ephemeral=True)
        return True

    async def send_panel(interaction, title, lines, footer):
        view = AuditPager(interaction.user.id, hosts, title, lines, page_size=5, footer_prefix=footer)
        view.message = await interaction_send(interaction, embed=view.build_embed(), view=view,
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="account", description="查玩家帳戶與可翻頁的完整總帳")
    @app_commands.describe(member="玩家", user_id="或填玩家 ID／提及")
    async def account(interaction: discord.Interaction, member: typing.Optional[discord.Member] = None,
                      user_id: typing.Optional[str] = None):
        if not await authorize(interaction):
            return
        target, error = await resolve(interaction, member, user_id, required=True, in_guild_only=False)
        if error:
            return await interaction_send(interaction, error, ephemeral=True)
        snapshot = await asyncio.to_thread(admin_audit_repo.account_snapshot, target.id, connect)
        if snapshot is None:
            return await interaction_send(interaction, "找不到玩家帳戶；查詢不會建立新帳戶。", ephemeral=True)
        view = AccountLedgerView(interaction.user.id, hosts, target.id, snapshot, connect, logger)
        view.message = await interaction_send(interaction, embed=view.build_embed(), view=view,
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="resets", description="查重置操作、操作者及調整前資料")
    @app_commands.describe(operation_id="操作編號；留空看最近 50 筆", member="篩選玩家", user_id="或填玩家 ID／提及")
    async def resets(interaction: discord.Interaction, operation_id: typing.Optional[str] = None,
                     member: typing.Optional[discord.Member] = None, user_id: typing.Optional[str] = None):
        if not await authorize(interaction):
            return
        uid = None
        if member is not None or user_id:
            target, error = await resolve(interaction, member, user_id, required=True, in_guild_only=False)
            if error:
                return await interaction_send(interaction, error, ephemeral=True)
            uid = target.id
        if operation_id:
            operation_id = operation_id.strip()
            if not operation_id or len(operation_id) > 96:
                return await interaction_send(interaction, "操作編號格式不正確。", ephemeral=True)
            detail = await asyncio.to_thread(admin_audit_repo.reset_details, operation_id, uid, connect)
            if detail is None:
                return await interaction_send(interaction, "找不到該重置操作。", ephemeral=True)
            operation, snapshots = detail
            target_balance = json.loads(operation[3]).get("target_balance")
            lines = []
            for player, raw, current in snapshots:
                before = json.loads(raw)
                current_text = "帳戶不存在" if current is None else f"{int(current):,}"
                lines.append(f"玩家 `{player}`｜重置前 {int(before['balance']):,} → 設定 {int(target_balance):,}\n"
                             f"目前餘額 {current_text}｜重置前戰績 {int(before.get('wins') or 0)} 勝")
            prefix = f"操作 {operation[0]}｜管理者 {operation[1]}"
            return await send_panel(interaction, "重置資料快照", lines or ["找不到符合條件的玩家快照。"],
                                    prefix + "｜最多顯示 100 位；可指定玩家")
        operations = await asyncio.to_thread(admin_audit_repo.reset_operations, uid, connect)
        lines = []
        for request, actor, action, raw, created in operations:
            result = json.loads(raw)
            label = "餘額清零" if action == "reset_zero" else "重置為 50,000"
            lines.append(f"操作 `{request}`｜{label}\n管理者 `{actor}`｜{safe_text(created,20)}\n"
                         f"{int(result['accounts'])} 個帳戶｜總調整 {int(result['delta']):+,}")
        await send_panel(interaction, "重置操作紀錄", lines or ["目前沒有符合條件的重置紀錄。"], "最近 50 筆｜僅供查閱")

    @group.command(name="pending", description="查看待結算／已逾時遊戲，不修改款項")
    async def pending(interaction: discord.Interaction):
        if not await authorize(interaction):
            return
        data = await asyncio.to_thread(admin_audit_repo.pending_settlements, connect)
        lines = []
        for sid, kind, phase, expires in data["rows"]:
            status = "等待結算" if phase == "settling" else "已逾時待退款"
            lines.append(f"`{safe_text(sid,96)}`｜{safe_text(kind,24)}｜{status}\n截止：<t:{int(expires)}:R>")
        roulette_pending = data.get("roulette_pending",0)
        if roulette_pending:
            lines.append(f"俄羅斯輪盤：{roulette_pending} 場待結算，系統會按已保存結果自動重試。")
        await send_panel(interaction, f"待處理結算：{data['total']+roulette_pending} 筆",
            lines or ["目前沒有待結算或逾時款項。"],
            f"輪盤對局 {data['roulette_count']} 場（可能正常進行中）｜最多 100 筆｜不會觸發付款")

    @group.command(name="reconcile", description="比對每個帳戶餘額與總帳流水合計")
    @app_commands.describe(set_baseline="把目前差額記為基準（需再按確認；不改變任何餘額）")
    async def reconcile(interaction: discord.Interaction, set_baseline: bool = False):
        if not await authorize(interaction):
            return
        report = await asyncio.to_thread(admin_audit_repo.reconcile_report, connect)
        summary = reconcile_summary(report)
        if set_baseline:
            view = BaselineConfirmView(interaction.user.id, hosts, connect, logger)
            embed = discord.Embed(title="設定對帳基準", color=0xED4245, description=(
                summary + "\n\n確認後，目前每個帳戶的差額都會被視為正常歷史；"
                "只記錄基準，不會修改任何餘額或流水。60 秒內有效。"))
            view.message = await interaction_send(interaction, embed=embed, view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
            return
        lines = [summary, ""] + reconcile_lines(report)
        if len(report.drifted) < report.accounts - report.matched:
            lines.append(f"（只列出差額最大的 {len(report.drifted)} 個）")
        await send_panel(interaction, "餘額對帳", lines, "唯讀｜差額 = 餘額 − 總帳合計 − 基準")

    @group.command(name="health", description="查看連線延遲、執行權檢查與背景任務")
    async def health(interaction: discord.Interaction):
        if not await authorize(interaction):
            return
        now = time.monotonic()
        seconds = max(0, int(now - getattr(bot, "runtime_started_at", now)))
        latency = bot.latency
        latency_text = f"{latency*1000:.0f} ms" if math.isfinite(latency) else "尚未取得"
        ready = getattr(bot, "_db_ready_event", None)
        initialized = ready is not None and ready.is_set()
        verified = getattr(getattr(bot, "instance_lease", None), "last_verified_at", None)
        lease_text = f"最近確認：{max(0,int(now-verified))} 秒前" if verified is not None else "尚未建立或已釋放"
        embed = discord.Embed(title="機器人健康狀態", color=0x5865F2)
        embed.description = (f"運行時間：{seconds//3600} 小時 {(seconds//60)%60} 分 {seconds%60} 秒\n"
                             f"Discord 延遲：{latency_text}\n資料庫初始化：{'完成' if initialized else '尚未完成'}\n"
                             f"執行權：{lease_text}")
        states = {"starting":"啟動中", "running":"運行中", "waiting":"等待恢復", "stopped":"已停止"}
        lines = [f"`{name}`：{states.get(s['state'],s['state'])}｜重啟 {s['restarts']} 次"
                 for name,s in getattr(bot, "background_task_status", {}).items()]
        embed.add_field(name="背景任務", value=("\n".join(lines) or "尚未啟動")[:1000], inline=False)
        embed.set_footer(text="執行權為最近檢查結果；此面板不會進行付款或重啟服務")
        await interaction_send(interaction, embed=embed, ephemeral=True,
                               allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="ai", description="查看 AI 聊天的設定、今日用量與估計花費")
    async def ai(interaction: discord.Interaction):
        if not await authorize(interaction):
            return
        chat = getattr(bot, "ai_chat", None)
        embed = discord.Embed(title="AI 聊天狀態", color=0x5865F2)
        if chat is None:
            embed.description = ("未啟用：Railway 沒有設定 `ANTHROPIC_API_KEY`，或 `AI_CHAT_ENABLED` 設為 false。\n"
                                 "設定後需重新部署才會生效。")
        else:
            embed.description = "\n".join(chat.status_lines())[:4000]
        embed.set_footer(text="用量記在記憶體，重啟後歸零；實際帳單以 Anthropic Console 為準")
        await interaction_send(interaction, embed=embed, ephemeral=True,
                               allowed_mentions=discord.AllowedMentions.none())

    bot.tree.add_command(group)
