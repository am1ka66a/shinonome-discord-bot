"""Pure rules regression tests: no Discord login, database or timers."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules.games import blackjack as bj
from bot_modules.games.roulette import RouletteState, hit_probability
from bot_modules.ui.blackjack import check_sidebets


def hand(ranks, suits=None):
    return [dict(rank=r, suit=s) for r, s in zip(ranks.split(), suits or ["H"] * len(ranks.split()))]


class BlackjackRulesTests(unittest.TestCase):
    def test_deck_and_flexible_aces(self):
        self.assertEqual(len(bj.get_deck()), 312)
        for cards, score in [("A A 9", 21), ("A A K", 12), ("A 6", 17), ("K Q 2", 22)]:
            self.assertEqual(bj.calculate_score(hand(cards)), score)

    def test_pair_and_every_trio_payout(self):
        self.assertEqual(bj.pair_outcome(hand("A A")), ("suited_pair", 30))
        self.assertEqual(bj.pair_outcome(hand("A A", "HS")), ("mixed_pair", 5))
        self.assertEqual(bj.pair_outcome(hand("A K")), ("miss", -1))
        cases = [("7 7 7", "HHH", "suited_triplet", 50),
                 ("A 2 3", "HHH", "straight_flush", 25),
                 ("7 7 7", "HSH", "triplet", 25),
                 ("Q K A", "HSH", "straight", 10),
                 ("A 2 3", "HSH", "straight", 10),
                 ("2 5 9", "HHH", "flush", 5),
                 ("K A 2", "HSH", "miss", -1)]
        for ranks, suits, kind, multiplier in cases:
            with self.subTest(ranks=ranks, suits=suits):
                self.assertEqual(bj.trio_outcome(hand(ranks, suits)), (kind, multiplier))

    def test_main_hand_priority_and_odd_bet_rounding(self):
        cases = [("2 3 4 5 6", "A K", "player_five", 252),
                 ("A K", "2 3 4 5 6", "dealer_five", -101),
                 ("2 3 4 5 6", "2 2 3 4 5", "five_tie", 0),
                 ("A K", "A Q", "blackjack_tie", 0),
                 ("A K", "K Q", "player_blackjack", 151),
                 ("K Q", "A K", "dealer_blackjack", -101),
                 ("K 9", "K Q 2", "win", 101),
                 ("K 9", "K Q", "lose", -101),
                 ("K 9", "Q 9", "tie", 0)]
        for player, dealer, kind, profit in cases:
            with self.subTest(kind=kind):
                self.assertEqual(bj.resolve_hand(hand(player), hand(dealer), 101), (kind, profit))

    def test_sidebet_text_and_experience_are_preserved(self):
        self.assertEqual(check_sidebets([], None, 0, 0), (0, ""))
        self.assertEqual(check_sidebets(hand("7 7"), hand("7")[0], 10, 20),
                         (1300, "🧧 同花對子！+300 🎯 21+3 同花三條！+1000 "))
        self.assertEqual(check_sidebets(hand("K A", "HS"), hand("2")[0], 10, 20),
                         (-30, "🧧 對子未中 -10 🎯 21+3 未中 -20 "))
        self.assertEqual([bj.experience_award(5, p) for p in (1, 0, -1)], [5, 2, 0])
        self.assertEqual(bj.experience_award(0, 0), 1)


class RouletteRulesTests(unittest.TestCase):
    def state(self, count=3, **kwargs):
        return RouletteState(match_id="test", mode="royale", guild_id=1, channel_id=2,
            bet_base=100, participant_ids=list(range(1, count+1)), stakes={i:100 for i in range(1,count+1)}, **kwargs)

    def test_three_to_six_player_gun_configuration_and_reload(self):
        for n in range(3, 7):
            state = self.state(n)
            self.assertEqual(state.bullet_positions, set())
            state.configure_royale_gun()
            self.assertEqual(state.chambers, 6+(n-2)*3)
            self.assertEqual(len(state.bullet_positions), n-1)
            self.assertTrue(all(0 <= pos < state.chambers for pos in state.bullet_positions))
            state.trigger_index = state.chambers
            state.alive_ids = [1, 2]
            self.assertTrue(state.ensure_chambers_for_royale())
            self.assertEqual((state.chambers, state.trigger_index, state.remaining_bullets()), (6, 0, 1))
        self.assertEqual(hit_probability(3, 1), 100/3)
        self.assertEqual(hit_probability(0, 1), 0)

    def test_turn_elimination_and_single_settlement_claim(self):
        state = self.state(turn_uid=2)
        self.assertIsNone(state.apply_royale_elimination(2))
        self.assertEqual((state.alive_ids, state.turn_uid), ([1, 3], 3))
        self.assertEqual(state.apply_royale_elimination(3), 1)
        self.assertFalse(state.mark_settled())
        other = self.state()
        self.assertTrue(other.mark_settled())
        self.assertFalse(other.mark_settled())

    def test_saved_state_and_legacy_single_bullet_roundtrip(self):
        state = self.state(bullet_positions=[1, 7], chambers=9, trigger_index=2,
                           turn_uid=3, history=["first"], lobby_started=True)
        record = dict(match_id="test", mode="royale", guild_id=1, channel_id=2, payload=state.to_payload())
        restored = RouletteState.from_record(record)
        self.assertEqual(restored.to_payload(), state.to_payload())
        self.assertEqual(restored.remaining_bullets(), 1)
        record["mode"] = "duel"
        record["payload"].pop("bullet_positions")
        record["payload"]["bullet_at"] = 4
        self.assertEqual(RouletteState.from_record(record).bullet_positions, {4})

    def test_lobby_departure_returns_stake_once(self):
        state = self.state()
        self.assertEqual(state.remove_participant(2), 100)
        self.assertEqual(state.remove_participant(2), 0)
        self.assertEqual((state.pot, state.alive_ids), (200, [1, 3]))
