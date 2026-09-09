from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

U16_MAX = 65535
HELD_SHARE = 0.40


@dataclass(frozen=True)
class WeightVector:
    uids: list[int]
    weights: list[int]
    floored: list[int] = field(default_factory=list)
    reserve_uid: int | None = None

    def payload(self) -> dict[str, object]:
        return {
            "uids": list(self.uids),
            "weights": list(self.weights),
            "floored": list(self.floored),
            "reserve_uid": self.reserve_uid,
        }


def normalise(scores: Mapping[int, float]) -> dict[int, float]:
    positive = {uid: value for uid, value in scores.items() if value > 0.0}
    total = sum(positive.values())
    if total <= 0.0:
        return {}
    return {uid: value / total for uid, value in positive.items()}


def with_reserve(shares: Mapping[int, float], reserve_uid: int | None, held_share: float = HELD_SHARE) -> dict[int, float]:
    if reserve_uid is None or held_share <= 0.0:
        return dict(shares)
    held = min(max(held_share, 0.0), 1.0)
    scaled = {uid: value * (1.0 - held) for uid, value in shares.items() if uid != reserve_uid}
    scaled[reserve_uid] = scaled.get(reserve_uid, 0.0) + held
    return scaled


def to_u16(shares: Mapping[int, float]) -> dict[int, int]:
    if not shares:
        return {}
    top = max(shares.values())
    if top <= 0.0:
        return {}
    out: dict[int, int] = {}
    for uid, value in shares.items():
        if value <= 0.0:
            continue
        quantised = round(value / top * U16_MAX)
        out[uid] = max(1, quantised)
    return out


def eligibility_floor(u16: dict[int, int], active_uids: set[int], donor_uid: int | None) -> list[int]:
    missing = sorted(uid for uid in active_uids if uid not in u16 and uid != donor_uid)
    if not missing or donor_uid is None or donor_uid not in u16:
        return []
    floored: list[int] = []
    for uid in missing:
        if u16[donor_uid] <= 1:
            break
        u16[donor_uid] -= 1
        u16[uid] = 1
        floored.append(uid)
    return floored


def build(
    scores: Mapping[int, float],
    active_uids: set[int] | None = None,
    reserve_uid: int | None = None,
    held_share: float = HELD_SHARE,
) -> WeightVector:
    shares = with_reserve(normalise(scores), reserve_uid, held_share)
    u16 = to_u16(shares)
    floored = eligibility_floor(u16, set(active_uids or ()), reserve_uid)
    ordered = sorted(u16.items())
    return WeightVector(
        uids=[uid for uid, _ in ordered],
        weights=[weight for _, weight in ordered],
        floored=floored,
        reserve_uid=reserve_uid,
    )


def version_key(version: str) -> int:
    parts = [int(part) for part in version.split(".")[:3]] + [0, 0, 0]
    major, minor, patch = parts[:3]
    return major * 10000 + minor * 100 + patch
