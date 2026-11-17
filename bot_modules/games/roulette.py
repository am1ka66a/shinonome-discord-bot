"""Pure roulette rules and serializable state; payouts stay in the controller."""
import random
import typing

RR_ROYALE_MIN_PLAYERS = 3
RR_ROYALE_MAX_PLAYERS = 6
RR_ROYALE_BASE_PLAYERS = 2
RR_ROYALE_CHAMBERS_PER_EXTRA_PLAYER = 3


def hit_probability(remaining_chambers, remaining_bullets):
    if remaining_chambers <= 0 or remaining_bullets <= 0:
        return 0.0
    return remaining_bullets * 100.0 / remaining_chambers


def royale_bullet_count(player_count):
    return max(1, int(player_count) - 1)


def royale_chamber_count(player_count, bullet_count, base_chambers=6):
    extra = max(0, int(player_count) - RR_ROYALE_BASE_PLAYERS)
    return max(base_chambers + extra * RR_ROYALE_CHAMBERS_PER_EXTRA_PLAYER, bullet_count)


class RouletteState:
    default_chambers = 6

    def __init__(
        self,
        *,
        match_id: str,
        mode: str,
        guild_id: int,
        channel_id: int,
        bet_base: int,
        participant_ids: typing.List[int],
        stakes: typing.Dict[int, int],
        chambers: typing.Optional[int] = None,
        bullet_positions: typing.Optional[typing.Iterable[int]] = None,
        bullet_at: typing.Optional[int] = None,
        trigger_index: int = 0,
        turn_uid: typing.Optional[int] = None,
        alive_ids: typing.Optional[typing.List[int]] = None,
        history: typing.Optional[typing.List[str]] = None,
        settled: bool = False,
        invite_resolved: bool = False,
        lobby_started: bool = False,
        host_id: typing.Optional[int] = None,
        challenger_id: typing.Optional[int] = None,
        opponent_id: typing.Optional[int] = None,
    ):
        self.match_id = match_id
        self.mode = mode
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.bet_base = int(bet_base)
        self.chambers = int(self.default_chambers if chambers is None else chambers)
        if bullet_positions is not None:
            self.bullet_positions = {int(x) for x in bullet_positions}
        elif bullet_at is not None:
            self.bullet_positions = {int(bullet_at)}
        elif mode == "royale" and not lobby_started:
            self.bullet_positions = set()
        else:
            self.bullet_positions = {random.randrange(self.chambers)}
        self.trigger_index = int(trigger_index)
        self.participant_ids = [int(x) for x in participant_ids]
        self.stakes = {int(k): int(v) for k, v in stakes.items()}
        self.alive_ids = list(alive_ids if alive_ids is not None else self.participant_ids)
        self.history = list(history or [])
        self.settled = bool(settled)
        self.invite_resolved = bool(invite_resolved)
        self.lobby_started = bool(lobby_started)
        self.host_id = host_id
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id
        self.turn_uid = turn_uid
        if self.turn_uid is None and self.mode == "duel" and self.invite_resolved:
            self.turn_uid = random.choice(self.participant_ids)


    @property
    def pot(self) -> int:
        return sum(self.stakes.values())


    @property
    def remaining_chambers(self) -> int:
        return max(0, self.chambers - self.trigger_index)


    def remaining_bullets(self) -> int:
        return sum(1 for pos in self.bullet_positions if pos >= self.trigger_index)


    def configure_royale_gun(self, *, use_alive: bool = False) -> None:
        player_count = len(self.alive_ids) if use_alive else len(self.participant_ids)
        bullet_count = royale_bullet_count(player_count)
        self.chambers = royale_chamber_count(player_count, bullet_count, self.default_chambers)
        self.bullet_positions = set(random.sample(range(self.chambers), bullet_count))
        self.trigger_index = 0


    def ensure_chambers_for_royale(self) -> bool:
        """若膛室打空但尚未分出勝負，重新装弹。"""
        if self.mode != "royale" or len(self.alive_ids) <= 1:
            return False
        if self.trigger_index < self.chambers:
            return False
        self.history.append("🔄 膛室打空，重新装弹！")
        self.configure_royale_gun(use_alive=True)
        return True


    def stake_of(self, uid: int) -> int:
        return int(self.stakes.get(int(uid), 0))


    def remove_participant(self, uid: int) -> int:
        """把玩家移出報名名單，回傳應退還的金額。"""
        uid = int(uid)
        refund = self.stake_of(uid)
        self.participant_ids = [x for x in self.participant_ids if x != uid]
        self.stakes.pop(uid, None)
        self.alive_ids = list(self.participant_ids)
        return refund


    def mention_line(self) -> str:
        return " ".join(f"<@{uid}>" for uid in self.participant_ids)


    def other_uid_duel(self, uid: int) -> int:
        return self.participant_ids[1] if uid == self.participant_ids[0] else self.participant_ids[0]


    def next_alive_uid(self, after_uid: int) -> int:
        if not self.alive_ids:
            return after_uid
        if after_uid not in self.alive_ids:
            return self.alive_ids[0]
        idx = self.alive_ids.index(after_uid)
        return self.alive_ids[(idx + 1) % len(self.alive_ids)]


    def to_payload(self) -> typing.Dict[str, typing.Any]:
        return {
            "bet_base": self.bet_base,
            "chambers": self.chambers,
            "bullet_positions": sorted(self.bullet_positions),
            "trigger_index": self.trigger_index,
            "turn_uid": self.turn_uid,
            "participant_ids": self.participant_ids,
            "stakes": {str(k): v for k, v in self.stakes.items()},
            "alive_ids": self.alive_ids,
            "history": self.history,
            "settled": self.settled,
            "invite_resolved": self.invite_resolved,
            "lobby_started": self.lobby_started,
            "host_id": self.host_id,
            "challenger_id": self.challenger_id,
            "opponent_id": self.opponent_id,
        }


    @classmethod
    def from_record(cls, record: typing.Dict[str, typing.Any]) -> "RouletteState":
        payload = record.get("payload") or {}
        stakes_raw = payload.get("stakes") or {}
        stakes = {int(k): int(v) for k, v in stakes_raw.items()}
        bullet_positions = payload.get("bullet_positions")
        legacy_bullet_at = payload.get("bullet_at")
        m = cls(
            match_id=str(record["match_id"]),
            mode=str(record["mode"]),
            guild_id=int(record["guild_id"]),
            channel_id=int(record["channel_id"]),
            bet_base=int(payload.get("bet_base") or 0),
            participant_ids=[int(x) for x in payload.get("participant_ids") or []],
            stakes=stakes,
            chambers=int(payload.get("chambers") or cls.default_chambers),
            bullet_positions=bullet_positions if bullet_positions is not None else None,
            bullet_at=int(legacy_bullet_at) if legacy_bullet_at is not None else None,
            trigger_index=int(payload.get("trigger_index") or 0),
            turn_uid=payload.get("turn_uid"),
            alive_ids=[int(x) for x in payload.get("alive_ids") or []],
            history=list(payload.get("history") or []),
            settled=bool(payload.get("settled")),
            invite_resolved=bool(payload.get("invite_resolved")),
            lobby_started=bool(payload.get("lobby_started")),
            host_id=payload.get("host_id"),
            challenger_id=payload.get("challenger_id"),
            opponent_id=payload.get("opponent_id"),
        )
        if m.turn_uid is not None:
            m.turn_uid = int(m.turn_uid)
        if m.host_id is not None:
            m.host_id = int(m.host_id)
        if m.challenger_id is not None:
            m.challenger_id = int(m.challenger_id)
        if m.opponent_id is not None:
            m.opponent_id = int(m.opponent_id)
        if (
            m.mode == "royale"
            and m.lobby_started
            and not m.bullet_positions
            and not m.settled
        ):
            m.configure_royale_gun(use_alive=True)
        return m


    def mark_settled(self) -> bool:
        """同步搶下結算權，回傳 True 代表由這次呼叫負責付錢。"""
        if self.settled:
            return False
        self.settled = True
        return True


    def apply_royale_elimination(self, uid: int) -> typing.Optional[int]:
        """同步淘汰玩家並推進輪次，回傳勝者（尚未分出勝負則為 None）。"""
        uid = int(uid)
        was_turn = self.turn_uid == uid
        next_turn = self.next_alive_uid(uid) if was_turn else self.turn_uid
        if uid in self.alive_ids:
            self.alive_ids = [x for x in self.alive_ids if x != uid]
        if len(self.alive_ids) == 1:
            self.settled = True
            return self.alive_ids[0]
        if was_turn and self.alive_ids:
            self.turn_uid = next_turn if next_turn in self.alive_ids else self.alive_ids[0]
        return None
