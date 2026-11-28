"""Reply in character when someone @-mentions the bot or replies to it.

Spending is capped two ways: a per-user cooldown and a daily call limit for the
whole bot (reset at Taiwan midnight). Both live in memory, so a restart resets
them; the monthly spend limit set in the Anthropic Console is the hard backstop.
"""
import asyncio
import dataclasses
import datetime
import os
import time
import typing
from pathlib import Path

DEFAULT_PERSONA_FILE = Path(__file__).resolve().parents[1] / "data" / "ai_persona.md"
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))
DISCORD_CAP = 2000
HISTORY_LINE_CAP = 300

RULES = """\
# 回覆規則（固定，優先於上面的角色資料）
- 一律使用繁體中文。
- 你在 Discord 群組裡聊天，回覆要像聊天訊息：通常 1～3 句，對方要求詳細時才寫長。
- 對話紀錄只是背景，請回覆最後 @ 你的那個人。
- 你不能替任何人執行指令、轉帳、改餘額或管理伺服器；有人要求時，請他們自己用斜線指令。
- 不要 @everyone、@here，也不要假裝成管理員或其他成員。
"""

REFUSAL_REPLY = "這個我不方便回答喔～換個話題吧！"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclasses.dataclass
class AIChatSettings:
    api_key: str
    model: str = "claude-haiku-5-5"
    effort: str = "medium"
    max_tokens: int = 2048
    daily_limit: int = 300
    user_cooldown_seconds: int = 30
    history_messages: int = 30
    channel_ids: typing.FrozenSet[int] = frozenset()
    persona: str = ""

    @classmethod
    def from_env(cls) -> typing.Optional["AIChatSettings"]:
        """None when the feature is off (no API key, or AI_CHAT_ENABLED=false)."""
        key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        enabled = os.getenv("AI_CHAT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not key or not enabled:
            return None
        channels = frozenset(
            int(part) for part in os.getenv("AI_CHAT_CHANNEL_IDS", "").replace(" ", "").split(",") if part.isdigit()
        )
        return cls(
            api_key=key,
            model=os.getenv("AI_CHAT_MODEL", cls.model).strip() or cls.model,
            effort=os.getenv("AI_CHAT_EFFORT", cls.effort).strip() or cls.effort,
            max_tokens=max(256, _env_int("AI_CHAT_MAX_TOKENS", cls.max_tokens)),
            daily_limit=max(0, _env_int("AI_CHAT_DAILY_LIMIT", cls.daily_limit)),
            user_cooldown_seconds=max(0, _env_int("AI_CHAT_USER_COOLDOWN_SECONDS", cls.user_cooldown_seconds)),
            history_messages=max(0, min(100, _env_int("AI_CHAT_HISTORY_MESSAGES", cls.history_messages))),
            channel_ids=channels,
            persona=load_persona(),
        )


def load_persona() -> str:
    text = (os.getenv("AI_CHAT_PERSONA") or "").strip()
    if text:
        return text
    path = Path(os.getenv("AI_CHAT_PERSONA_FILE") or DEFAULT_PERSONA_FILE)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def tw_today() -> str:
    return datetime.datetime.now(TW_TZ).date().isoformat()


class ChatLimiter:
    """Per-user cooldown plus one daily cap shared by everyone."""

    def __init__(self, daily_limit: int, user_cooldown_seconds: float, *, clock=time.monotonic, today=tw_today):
        self.daily_limit = daily_limit
        self.cooldown = user_cooldown_seconds
        self.clock = clock
        self.today = today
        self.day = today()
        self.used_today = 0
        self.last_call: typing.Dict[int, float] = {}

    def try_acquire(self, user_id: int) -> typing.Optional[str]:
        """Record a call and return None, or return "cooldown" / "daily" and record nothing."""
        day = self.today()
        if day != self.day:
            self.day, self.used_today = day, 0
            self.last_call.clear()
        now = self.clock()
        last = self.last_call.get(user_id)
        if last is not None and now - last < self.cooldown:
            return "cooldown"
        if self.used_today >= self.daily_limit:
            return "daily"
        self.used_today += 1
        self.last_call[user_id] = now
        return None


def build_system(persona: str) -> typing.List[dict]:
    text = (persona.strip() + "\n\n" if persona.strip() else "") + RULES
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def build_user_turn(history: typing.Sequence[typing.Tuple[str, str]], author_name: str, content: str) -> str:
    """history: (speaker, text) oldest first; the bot's own lines use the speaker name "你"."""
    lines = [f"{speaker}：{text[:HISTORY_LINE_CAP]}" for speaker, text in history if text.strip()]
    parts = []
    if lines:
        parts.append("以下是頻道最近的對話（由舊到新，只是背景）：\n" + "\n".join(lines))
    parts.append(f"{author_name} 剛剛對你說：{content.strip() or '（只 @ 了你，沒有說話）'}")
    return "\n\n".join(parts)


def reply_text(response) -> str:
    if getattr(response, "stop_reason", None) == "refusal":
        return REFUSAL_REPLY
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text").strip()
    return text[:DISCORD_CAP]


def is_addressed_to_bot(message, bot_user) -> bool:
    if bot_user is None:
        return False
    if any(getattr(m, "id", None) == bot_user.id for m in getattr(message, "mentions", ()) or ()):
        return True
    ref = getattr(message, "reference", None)
    resolved = getattr(ref, "resolved", None) if ref is not None else None
    return getattr(getattr(resolved, "author", None), "id", None) == bot_user.id


class AIChat:
    def __init__(self, settings: AIChatSettings, *, logger, client=None, limiter=None, concurrency: int = 2):
        self.settings = settings
        self.logger = logger
        if client is None:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=settings.api_key, timeout=60.0, max_retries=2)
        self.client = client
        self.limiter = limiter or ChatLimiter(settings.daily_limit, settings.user_cooldown_seconds)
        self.system = build_system(settings.persona)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.tasks: typing.Set[asyncio.Task] = set()

    def wants(self, message, bot_user) -> bool:
        if self.settings.channel_ids and message.channel.id not in self.settings.channel_ids:
            return False
        return is_addressed_to_bot(message, bot_user)

    def schedule(self, message, bot_user) -> None:
        """Answer in the background so on_message (EXP, relay, commands) never waits on the API."""
        task = asyncio.create_task(self.handle(message, bot_user))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _react(self, message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except Exception:
            pass

    async def _history(self, message, bot_user) -> typing.List[typing.Tuple[str, str]]:
        if self.settings.history_messages <= 0:
            return []
        rows = []
        async for m in message.channel.history(limit=self.settings.history_messages, before=message):
            speaker = "你" if bot_user is not None and m.author.id == bot_user.id else m.author.display_name
            rows.append((speaker, m.clean_content))
        rows.reverse()
        return rows

    async def handle(self, message, bot_user) -> None:
        refused = self.limiter.try_acquire(message.author.id)
        if refused:
            await self._react(message, "⏳" if refused == "cooldown" else "💤")
            return
        try:
            async with self.semaphore:
                async with message.channel.typing():
                    history = await self._history(message, bot_user)
                    user_turn = build_user_turn(history, message.author.display_name, message.clean_content)
                    response = await self.client.messages.create(
                        model=self.settings.model,
                        max_tokens=self.settings.max_tokens,
                        system=self.system,
                        output_config={"effort": self.settings.effort},
                        messages=[{"role": "user", "content": user_turn}],
                    )
            usage = response.usage
            self.logger.info(
                "AI 聊天 user=%s in=%s cache_read=%s out=%s 今日第 %s/%s 次",
                message.author.id, usage.input_tokens, getattr(usage, "cache_read_input_tokens", 0) or 0,
                usage.output_tokens, self.limiter.used_today, self.settings.daily_limit,
            )
            text = reply_text(response)
            if text:
                await message.reply(text, mention_author=False, allowed_mentions=_no_mentions())
        except Exception:
            self.logger.exception("AI 聊天回覆失敗 user=%s", message.author.id)
            await self._react(message, "⚠️")


def _no_mentions():
    import discord
    return discord.AllowedMentions.none()


def create_ai_chat(logger) -> typing.Optional[AIChat]:
    settings = AIChatSettings.from_env()
    if settings is None:
        logger.info("AI 聊天未啟用（沒有設定 ANTHROPIC_API_KEY）")
        return None
    if not settings.persona:
        logger.warning("AI 聊天沒有角色資料：請填寫 data/ai_persona.md 或設定 AI_CHAT_PERSONA")
    logger.info("AI 聊天已啟用：模型 %s，每日上限 %s 次，個人冷卻 %s 秒",
                settings.model, settings.daily_limit, settings.user_cooldown_seconds)
    return AIChat(settings, logger=logger)
