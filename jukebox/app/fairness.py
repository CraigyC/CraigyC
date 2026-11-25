"""The fairness rules, kept free of I/O so they are easy to test and tune.

Importance
    A user's 1st track has importance 1.0, and each further track they add is
    worth less:  importance(n) = 1 / (1 + decay * (n - 1)).
    With the default decay of 1.0 that is 1, 1/2, 1/3, 1/4 ...

Shuffling into the queue
    A new track is dropped into a random slot of the upcoming queue. The slot is
    chosen as  fraction = u ** importance  (u uniform in [0, 1)), so:
      * importance 1.0  -> any slot is equally likely
      * importance 0.5  -> skewed towards the back
      * importance 0.1  -> almost always near the end
    Tracks that won a "play next" request sit at the front and are never jumped.

Play-next requests
    A user's k-th request succeeds with probability 1 / (1 + decay * (k - 1)):
    100%, 50%, 33%, 25% ... with the default decay. Every attempt counts, won or lost.
"""
from __future__ import annotations

import random


def importance(tracks_already_added: int, decay: float = 1.0) -> float:
    return 1.0 / (1.0 + max(decay, 0.0) * max(tracks_already_added, 0))


def play_next_chance(requests_already_made: int, decay: float = 1.0) -> float:
    return 1.0 / (1.0 + max(decay, 0.0) * max(requests_already_made, 0))


def insertion_index(
    queue_length: int,
    weight: float,
    protected_front: int = 0,
    rng: random.Random | None = None,
) -> int:
    """Index (0 = next to play) at which a new track of the given importance goes."""
    rng = rng or random
    protected_front = max(0, min(protected_front, queue_length))
    slots = queue_length - protected_front + 1  # gaps available behind the protected block
    fraction = rng.random() ** max(weight, 1e-6)
    return protected_front + min(int(fraction * slots), slots - 1)


def roll_play_next(requests_already_made: int, decay: float = 1.0, rng: random.Random | None = None) -> tuple[bool, float]:
    chance = play_next_chance(requests_already_made, decay)
    return (rng or random).random() < chance, chance
