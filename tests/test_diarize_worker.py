"""merge_to_count: turning the engine's threshold clusters into the count asked for.

Hand-made voice prints stand in for real embeddings: direction is the voice,
length is irrelevant (similarity is cosine).
"""

from __future__ import annotations

import math

from transcriptor_prime.diarize_worker import merge_to_count


def at(degrees: float, weight: float = 1.0) -> list[float]:
    return [weight * math.cos(math.radians(degrees)), weight * math.sin(math.radians(degrees))]


def seconds_by_speaker(turns) -> dict[int, float]:
    totals: dict[int, float] = {}
    for start, end, who in turns:
        totals[who] = totals.get(who, 0.0) + (end - start)
    return totals


def test_one_person_split_in_two_is_joined_back_together():
    # The shape of the recording that exposed the bug: a host the engine split
    # in two (1 and 0, nearly identical), two guests (4 and 12), and scraps.
    turns = [
        (0.0, 275.0, 0),
        (275.0, 820.0, 1),
        (820.0, 2282.0, 4),
        (2282.0, 2791.0, 12),
        (2791.0, 2840.0, 14),  # scrap that sounds like guest 12
        (2840.0, 2879.0, 5),   # scrap that sounds like guest 4
    ]
    centroids = {0: at(0), 1: at(5), 4: at(60), 12: at(120), 14: at(118), 5: at(58)}

    merged = merge_to_count(turns, centroids, 3)

    labels = [who for _, _, who in merged]
    assert labels[0] == labels[1]  # the host, once
    assert len(set(labels)) == 3
    assert labels[4] == labels[3]
    assert labels[5] == labels[2]


def test_a_stray_fragment_never_takes_one_of_the_places():
    # The fragment is unlike everyone — exactly what the engine's own forced
    # count gave a slot to, fusing two real people to make room.
    turns = [(0.0, 600.0, 0), (600.0, 1200.0, 1), (1200.0, 1800.0, 2), (1800.0, 1802.0, 9)]
    centroids = {0: at(0), 1: at(10), 2: at(90), 9: at(225)}

    merged = merge_to_count(turns, centroids, 2)

    assert len(seconds_by_speaker(merged)) == 2
    labels = [who for _, _, who in merged]
    assert labels[0] == labels[1]  # the two alike voices merged…
    assert labels[2] != labels[0]  # …and the distinct one kept


def test_a_scrap_goes_to_the_voice_it_sounds_like_not_the_biggest():
    turns = [(0.0, 1000.0, 0), (1000.0, 1300.0, 1), (1300.0, 1305.0, 2)]
    centroids = {0: at(0), 1: at(90), 2: at(88)}

    merged = merge_to_count(turns, centroids, 2)

    assert merged[2][2] == merged[1][2]


def test_no_more_clusters_than_asked_for_is_left_alone():
    turns = [(0.0, 5.0, 3), (5.0, 9.0, 8)]
    assert merge_to_count(turns, {3: at(0), 8: at(90)}, 3) == turns
    assert merge_to_count(turns, {3: at(0), 8: at(90)}, 2) == turns


def test_a_cluster_too_short_to_embed_is_folded_in():
    turns = [(0.0, 300.0, 0), (300.0, 600.0, 1), (600.0, 900.0, 2), (900.0, 900.6, 3)]
    centroids = {0: at(0), 1: at(90), 2: at(180)}  # 3 had nothing a second long

    merged = merge_to_count(turns, centroids, 3)

    assert len(seconds_by_speaker(merged)) == 3
    assert [who for _, _, who in merged[:3]] == [0, 1, 2]
