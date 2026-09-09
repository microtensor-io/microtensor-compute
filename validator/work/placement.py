from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from protocol.session import JobRecord
from validator.data.tiers import PRIORITY, WorkKind
from validator.scoring.reliability import UptimeLedger

VERIFIED_STATES = ("probation", "active")
RANK: dict[WorkKind, int] = {kind: rank for rank, kind in enumerate(PRIORITY)}
SENSITIVE = {WorkKind.RENTAL}
CUSTOMER = {WorkKind.INFERENCE}


@dataclass(frozen=True)
class Placement:
    rig_id: str = ""
    gpu_uuids: list[str] = field(default_factory=list)
    displaced: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.rig_id and self.gpu_uuids)

    def payload(self) -> dict[str, Any]:
        return {
            "rig_id": self.rig_id,
            "gpu_uuids": list(self.gpu_uuids),
            "displaced": list(self.displaced),
            "reason": self.reason,
        }


def kind_of(text: str) -> WorkKind | None:
    try:
        return WorkKind(text)
    except ValueError:
        return None


def ineligible_reason(rig: dict[str, Any], kind: WorkKind) -> str:
    if str(rig.get("state", "")) not in VERIFIED_STATES:
        return f"rig is {rig.get('state', 'unknown')}"
    if not rig.get("online", False):
        return "rig is offline"
    if rig.get("draining", False):
        return "rig is draining"
    if kind is WorkKind.RENTAL and rig.get("rental_opt_out", False):
        return "rental declined by the owner"
    kinds = (rig.get("eligibility") or {}).get("kinds") or {}
    entry = kinds.get(kind.value)
    if entry is None:
        return f"no eligibility record for {kind.value}"
    if not entry.get("eligible", False):
        reasons = entry.get("reasons") or []
        return f"not eligible for {kind.value}: {reasons[0] if reasons else 'unspecified'}"
    return ""


def occupancy(rig: dict[str, Any], jobs: list[JobRecord]) -> dict[str, list[JobRecord]]:
    uuids = [
        str(card.get("uuid", ""))
        for card in rig.get("gpus") or []
        if isinstance(card, dict) and card.get("uuid")
    ]
    table: dict[str, list[JobRecord]] = {uuid: [] for uuid in uuids}
    for job in jobs:
        targets = [job.gpu_uuid] if job.gpu_uuid and job.gpu_uuid in table else uuids
        for uuid in targets:
            table[uuid].append(job)
    return table


def can_share(existing: list[JobRecord], kind: WorkKind) -> bool:
    if not existing:
        return True
    kinds = {kind_of(job.kind) for job in existing}
    if kind in SENSITIVE or any(k in SENSITIVE for k in kinds):
        return False
    if kind in CUSTOMER:
        return kinds <= CUSTOMER
    return False


def can_displace(existing: list[JobRecord], kind: WorkKind) -> bool:
    if not existing:
        return False
    for job in existing:
        current = kind_of(job.kind)
        if current is None or RANK[current] <= RANK[kind]:
            return False
        if current in SENSITIVE or current in CUSTOMER:
            return False
    return True


def rank_rig(
    rig: dict[str, Any], free: int, displaced: int, ledger: UptimeLedger | None
) -> tuple[float, ...]:
    reliability = float(rig.get("reliability", 0.0) or 0.0)
    uptime_days = ledger.uptime_days(str(rig.get("id", ""))) if ledger is not None else 0.0
    return (float(displaced), -reliability, -uptime_days, -float(free))


def choose(
    kind_text: str,
    gpu_count: int,
    roster: list[dict[str, Any]],
    jobs_by_rig: dict[str, list[JobRecord]],
    ledger: UptimeLedger | None = None,
    whole_node: bool = False,
) -> Placement:
    kind = kind_of(kind_text)
    if kind is None:
        return Placement(reason=f"unknown work kind {kind_text!r}")
    needed = max(1, int(gpu_count))
    reasons: list[str] = []
    candidates: list[tuple[tuple[float, ...], Placement]] = []
    for rig in roster:
        rig_id = str(rig.get("id", "") or "")
        reason = ineligible_reason(rig, kind)
        if reason:
            reasons.append(f"{rig_id}: {reason}")
            continue
        table = occupancy(rig, jobs_by_rig.get(rig_id, []))
        if whole_node and len(table) != needed:
            reasons.append(f"{rig_id}: whole node has {len(table)} cards, {needed} requested")
            continue
        free = [uuid for uuid, existing in table.items() if not existing]
        shareable = [
            uuid for uuid, existing in table.items() if existing and can_share(existing, kind)
        ]
        displaceable = [
            uuid for uuid, existing in table.items() if existing and can_displace(existing, kind)
        ]
        chosen = free[:needed]
        if len(chosen) < needed:
            chosen += shareable[: needed - len(chosen)]
        displaced: list[str] = []
        if len(chosen) < needed:
            for uuid in displaceable[: needed - len(chosen)]:
                chosen.append(uuid)
                displaced.extend(job.job_id for job in table[uuid])
        if len(chosen) < needed:
            reasons.append(f"{rig_id}: only {len(chosen)} of {needed} cards available")
            continue
        placement = Placement(rig_id, chosen, sorted(set(displaced)))
        candidates.append(
            (rank_rig(rig, len(free) - len(chosen), len(displaced), ledger), placement)
        )
    if not candidates:
        return Placement(reason="; ".join(reasons[:8]) or "no rigs in the roster")
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]
