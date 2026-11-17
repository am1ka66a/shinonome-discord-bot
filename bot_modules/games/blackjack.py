"""Pure blackjack deck and scoring rules."""


def get_deck(num_decks=6):
    suits = ["♥️", "♦️", "♣️", "♠️"]
    ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    return [{"rank": r, "suit": s} for s in suits for r in ranks] * num_decks


def calculate_score(hand):
    score, aces = 0, 0
    values = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10, "J": 10, "Q": 10, "K": 10, "A": 11}
    for c in hand:
        score += values[c["rank"]]
        if c["rank"] == "A":
            aces += 1
    while score > 21 and aces:
        score -= 10
        aces -= 1
    return score


def pair_outcome(cards):
    first, second = cards[:2]
    if first["rank"] != second["rank"]:
        return "miss", -1
    return ("suited_pair", 30) if first["suit"] == second["suit"] else ("mixed_pair", 5)


def trio_outcome(cards):
    ranks = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
             "8": 8, "9": 9, "10": 10, "J": 11, "Q": 12, "K": 13, "A": 14}
    values = sorted(ranks[c["rank"]] for c in cards)
    if values == [2, 3, 14]:
        values = [1, 2, 3]
    flush = len({c["suit"] for c in cards}) == 1
    straight = values[2] - values[1] == 1 and values[1] - values[0] == 1
    triplet = len({c["rank"] for c in cards}) == 1
    if flush and triplet:
        return "suited_triplet", 50
    if flush and straight:
        return "straight_flush", 25
    if triplet:
        return "triplet", 25
    if straight:
        return "straight", 10
    if flush:
        return "flush", 5
    return "miss", -1


def resolve_hand(player, dealer, bet):
    """Resolve an unfinished hand; busted/surrendered hands are handled before this."""
    player_score, dealer_score = calculate_score(player), calculate_score(dealer)
    player_five = len(player) == 5 and player_score <= 21
    dealer_five = len(dealer) == 5 and dealer_score <= 21
    player_bj = len(player) == 2 and player_score == 21
    dealer_bj = len(dealer) == 2 and dealer_score == 21
    if player_five and dealer_five:
        return "five_tie", 0
    if player_five:
        return "player_five", int(bet * 2.5)
    if dealer_five:
        return "dealer_five", -bet
    if player_bj and dealer_bj:
        return "blackjack_tie", 0
    if player_bj:
        return "player_blackjack", int(bet * 1.5)
    if dealer_bj:
        return "dealer_blackjack", -bet
    if dealer_score > 21 or player_score > dealer_score:
        return "win", bet
    if player_score < dealer_score:
        return "lose", -bet
    return "tie", 0


def experience_award(base, profit):
    return base if profit > 0 else max(1, base // 2) if profit == 0 else 0
