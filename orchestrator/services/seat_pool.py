"""Pure, serializable pre-warmed seat-pool state machine.

This module performs no I/O.  A caller loads one JSON snapshot, applies a
command with an ``expected_revision``, then persists the returned snapshot with
an atomic compare-and-swap.  Command receipts make retries idempotent, while the
revision makes concurrent writers detectable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from typing import Any, Callable


DEFAULT_RELEASE_LEAD_MS = 5 * 60 * 1000
SEAT_STATES = (
    "planned",
    "warming",
    "verified",
    "reserved",
    "released",
    "frozen",
    "collected",
)


class SeatPoolError(RuntimeError):
    pass


class PoolConfigurationError(ValueError, SeatPoolError):
    pass


class RevisionConflictError(SeatPoolError):
    pass


class CommandConflictError(SeatPoolError):
    pass


class SeatStateError(SeatPoolError):
    pass


class CapacityExceededError(SeatPoolError):
    pass


class NoVerifiedSeatError(SeatPoolError):
    pass


class TooEarlyToReleaseError(SeatPoolError):
    pass


class TeacherApprovalRequiredError(SeatPoolError):
    pass


class NoSpareSeatError(SeatPoolError):
    pass


@dataclass(frozen=True)
class Seat:
    slot_no: int
    seat_key: str
    role: str
    state: str = "planned"
    uid: int | None = None
    uname: str = ""
    container_ref: str = ""
    image_digest: str = ""
    material_digest: str = ""
    failure_count: int = 0
    last_error: str = ""
    reserved_at_ms: int | None = None
    released_at_ms: int | None = None
    frozen_at_ms: int | None = None
    collected_at_ms: int | None = None


@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    fingerprint: str
    revision: int
    value: dict[str, Any]


@dataclass(frozen=True)
class TransitionResult:
    state: "SeatPoolState"
    value: dict[str, Any]
    replayed: bool = False


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(operation: str, arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical({"operation": operation, "arguments": arguments}).encode("utf-8")
    ).hexdigest()


def _validate_int(name: str, value: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PoolConfigurationError(f"{name} must be an integer >= {minimum}")
    return value


def reservation_state(
    *, now_ms: int, release_at_ms: int, begin_at_ms: int, teacher_approved: bool = False
) -> str:
    """Return the initial participant state at a schedule boundary."""
    if now_ms >= begin_at_ms and not teacher_approved:
        raise TeacherApprovalRequiredError(
            "contest has started; teacher approval is required"
        )
    return "released" if now_ms >= release_at_ms else "reserved"


@dataclass(frozen=True)
class SeatPoolState:
    schema_version: int
    tid: str
    max_participants: int
    spare_count: int
    begin_at_ms: int
    release_at_ms: int
    revision: int
    seats: tuple[Seat, ...]
    receipts: tuple[CommandReceipt, ...] = ()

    @classmethod
    def create(
        cls,
        tid: str,
        *,
        max_participants: int,
        spare_count: int,
        begin_at_ms: int,
        release_lead_ms: int = DEFAULT_RELEASE_LEAD_MS,
    ) -> "SeatPoolState":
        if not isinstance(tid, str) or not tid.strip():
            raise PoolConfigurationError("tid cannot be empty")
        maximum = _validate_int("max_participants", max_participants, minimum=1)
        spares = _validate_int("spare_count", spare_count)
        begin = _validate_int("begin_at_ms", begin_at_ms, minimum=1)
        lead = _validate_int("release_lead_ms", release_lead_ms)
        if spares > maximum:
            raise PoolConfigurationError("spare_count cannot exceed max_participants")
        if lead >= begin:
            raise PoolConfigurationError("release time cannot precede Unix epoch")
        seats = tuple(
            Seat(
                slot_no=index,
                seat_key=f"seat-{index:03d}",
                role="primary" if index <= maximum else "spare",
            )
            for index in range(1, maximum + spares + 1)
        )
        return cls(1, tid.strip(), maximum, spares, begin, begin - lead, 0, seats)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SeatPoolState":
        if raw.get("schema_version") != 1:
            raise PoolConfigurationError("unsupported seat-pool schema version")
        return cls(
            schema_version=1,
            tid=raw["tid"],
            max_participants=int(raw["max_participants"]),
            spare_count=int(raw["spare_count"]),
            begin_at_ms=int(raw["begin_at_ms"]),
            release_at_ms=int(raw["release_at_ms"]),
            revision=int(raw["revision"]),
            seats=tuple(Seat(**seat) for seat in raw.get("seats", [])),
            receipts=tuple(CommandReceipt(**receipt) for receipt in raw.get("receipts", [])),
        )

    @classmethod
    def from_json(cls, raw: str) -> "SeatPoolState":
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PoolConfigurationError("invalid seat-pool JSON") from exc
        if not isinstance(value, dict):
            raise PoolConfigurationError("seat-pool JSON must be an object")
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tid": self.tid,
            "max_participants": self.max_participants,
            "spare_count": self.spare_count,
            "begin_at_ms": self.begin_at_ms,
            "release_at_ms": self.release_at_ms,
            "revision": self.revision,
            "seats": [asdict(seat) for seat in self.seats],
            "receipts": [asdict(receipt) for receipt in self.receipts],
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict())

    def seat(self, slot_no: int) -> Seat:
        for seat in self.seats:
            if seat.slot_no == slot_no:
                return seat
        raise KeyError(f"unknown seat {slot_no}")

    def assignment(self, uid: int) -> Seat | None:
        return next((seat for seat in self.seats if seat.uid == uid), None)

    def state_counts(self) -> dict[str, int]:
        counts = {state: 0 for state in SEAT_STATES}
        for seat in self.seats:
            counts[seat.state] += 1
        return counts

    def _apply(
        self,
        *,
        operation: str,
        arguments: dict[str, Any],
        command_id: str,
        expected_revision: int,
        mutate: Callable[[list[Seat]], tuple[list[Seat], dict[str, Any], dict[str, int]]],
    ) -> TransitionResult:
        if not isinstance(command_id, str) or not command_id.strip():
            raise PoolConfigurationError("command_id cannot be empty")
        fingerprint = _fingerprint(operation, arguments)
        for receipt in self.receipts:
            if receipt.command_id == command_id:
                if receipt.fingerprint != fingerprint:
                    raise CommandConflictError(
                        "command_id was reused with different arguments"
                    )
                return TransitionResult(self, receipt.value, replayed=True)
        if expected_revision != self.revision:
            raise RevisionConflictError(
                f"expected revision {expected_revision}, current revision {self.revision}"
            )
        seats, value, pool_updates = mutate(list(self.seats))
        revision = self.revision + 1
        result_value = dict(value)
        result_value["revision"] = revision
        receipt = CommandReceipt(command_id, fingerprint, revision, result_value)
        state = replace(
            self,
            revision=revision,
            seats=tuple(seats),
            receipts=self.receipts + (receipt,),
            **pool_updates,
        )
        return TransitionResult(state, result_value)

    @staticmethod
    def _replace_seat(seats: list[Seat], updated: Seat) -> list[Seat]:
        return [updated if seat.slot_no == updated.slot_no else seat for seat in seats]

    def mark_warming(
        self, slot_no: int, *, now_ms: int, command_id: str, expected_revision: int
    ) -> TransitionResult:
        args = {"slot_no": slot_no, "now_ms": now_ms}

        def mutate(seats):
            seat = self.seat(slot_no)
            if seat.state not in {"planned", "warming", "verified"}:
                raise SeatStateError(f"cannot warm a {seat.state} seat")
            updated = seat if seat.state != "planned" else replace(seat, state="warming")
            return self._replace_seat(seats, updated), asdict(updated), {}

        return self._apply(operation="mark_warming", arguments=args,
                           command_id=command_id, expected_revision=expected_revision,
                           mutate=mutate)

    def mark_verified(
        self,
        slot_no: int,
        *,
        container_ref: str,
        image_digest: str,
        material_digest: str,
        now_ms: int,
        command_id: str,
        expected_revision: int,
    ) -> TransitionResult:
        evidence = (container_ref.strip(), image_digest.strip(), material_digest.strip())
        if any(not item for item in evidence):
            raise PoolConfigurationError("all verification evidence is required")
        args = {"slot_no": slot_no, "container_ref": evidence[0],
                "image_digest": evidence[1], "material_digest": evidence[2],
                "now_ms": now_ms}

        def mutate(seats):
            seat = self.seat(slot_no)
            if seat.state == "verified":
                actual = (seat.container_ref, seat.image_digest, seat.material_digest)
                if actual != evidence:
                    raise CommandConflictError("verified evidence cannot be changed")
                updated = seat
            else:
                if seat.state != "warming":
                    raise SeatStateError(f"cannot verify a {seat.state} seat")
                if any(other.container_ref == evidence[0] for other in seats
                       if other.slot_no != slot_no and other.container_ref):
                    raise PoolConfigurationError("container_ref must be unique")
                updated = replace(seat, state="verified", container_ref=evidence[0],
                                  image_digest=evidence[1], material_digest=evidence[2],
                                  last_error="")
            return self._replace_seat(seats, updated), asdict(updated), {}

        return self._apply(operation="mark_verified", arguments=args,
                           command_id=command_id, expected_revision=expected_revision,
                           mutate=mutate)

    def reserve(
        self,
        uid: int,
        uname: str,
        *,
        now_ms: int,
        teacher_approved: bool = False,
        command_id: str,
        expected_revision: int,
    ) -> TransitionResult:
        _validate_int("uid", uid, minimum=1)
        uname = uname.strip()
        if not uname:
            raise PoolConfigurationError("uname cannot be empty")
        args = {"uid": uid, "uname": uname, "now_ms": now_ms,
                "teacher_approved": bool(teacher_approved)}

        def mutate(seats):
            existing = next((seat for seat in seats if seat.uid == uid), None)
            if existing:
                if existing.uname != uname:
                    raise CommandConflictError("uid already has another username")
                return seats, asdict(existing), {}
            target = reservation_state(now_ms=now_ms,
                                       release_at_ms=self.release_at_ms,
                                       begin_at_ms=self.begin_at_ms,
                                       teacher_approved=teacher_approved)
            if sum(seat.uid is not None for seat in seats) >= self.max_participants:
                raise CapacityExceededError("maximum participant count reached")
            available = sorted(
                (seat for seat in seats if seat.state == "verified" and seat.uid is None),
                key=lambda seat: (seat.role == "spare", seat.slot_no),
            )
            if not available:
                raise NoVerifiedSeatError("no verified seat is available")
            chosen = available[0]
            updated = replace(chosen, state=target, uid=uid, uname=uname,
                              reserved_at_ms=now_ms,
                              released_at_ms=now_ms if target == "released" else None)
            return self._replace_seat(seats, updated), asdict(updated), {}

        return self._apply(operation="reserve", arguments=args, command_id=command_id,
                           expected_revision=expected_revision, mutate=mutate)

    def release(
        self, uid: int, *, now_ms: int, command_id: str, expected_revision: int
    ) -> TransitionResult:
        args = {"uid": uid, "now_ms": now_ms}

        def mutate(seats):
            seat = next((item for item in seats if item.uid == uid), None)
            if seat is None:
                raise KeyError(f"uid {uid} has no seat")
            if seat.state in {"released", "frozen", "collected"}:
                return seats, asdict(seat), {}
            if seat.state != "reserved":
                raise SeatStateError(f"cannot release a {seat.state} seat")
            if now_ms < self.release_at_ms:
                raise TooEarlyToReleaseError(
                    f"cannot release before {self.release_at_ms}"
                )
            updated = replace(seat, state="released", released_at_ms=now_ms)
            return self._replace_seat(seats, updated), asdict(updated), {}

        return self._apply(operation="release", arguments=args, command_id=command_id,
                           expected_revision=expected_revision, mutate=mutate)

    def release_due(
        self, *, now_ms: int, command_id: str, expected_revision: int
    ) -> TransitionResult:
        args = {"now_ms": now_ms}

        def mutate(seats):
            if now_ms < self.release_at_ms:
                return seats, {"released": []}, {}
            released = []
            output = []
            for seat in seats:
                if seat.state == "reserved":
                    seat = replace(seat, state="released", released_at_ms=now_ms)
                    released.append(asdict(seat))
                output.append(seat)
            return output, {"released": released}, {}

        return self._apply(operation="release_due", arguments=args,
                           command_id=command_id, expected_revision=expected_revision,
                           mutate=mutate)

    def replace_failed(
        self,
        slot_no: int,
        *,
        reason: str,
        now_ms: int,
        teacher_approved: bool = False,
        command_id: str,
        expected_revision: int,
    ) -> TransitionResult:
        reason = reason.strip()
        if not reason:
            raise PoolConfigurationError("failure reason cannot be empty")
        args = {"slot_no": slot_no, "reason": reason, "now_ms": now_ms,
                "teacher_approved": bool(teacher_approved)}

        def mutate(seats):
            failed = self.seat(slot_no)
            if failed.state in {"frozen", "collected"}:
                raise SeatStateError(f"cannot replace a {failed.state} seat")
            if failed.state == "planned" and failed.uid is None:
                return seats, {"replacement": None}, {}
            replacement = None
            target = None
            if failed.uid is not None:
                target = reservation_state(now_ms=now_ms,
                                           release_at_ms=self.release_at_ms,
                                           begin_at_ms=self.begin_at_ms,
                                           teacher_approved=teacher_approved)
                if failed.state == "released":
                    target = "released"
                replacement = next(
                    (seat for seat in seats if seat.role == "spare"
                     and seat.state == "verified" and seat.uid is None), None
                )
                if replacement is None:
                    raise NoSpareSeatError("no verified spare is available")
            reset = replace(failed, state="planned", uid=None, uname="",
                            container_ref="", image_digest="", material_digest="",
                            failure_count=failed.failure_count + 1, last_error=reason,
                            reserved_at_ms=None, released_at_ms=None,
                            frozen_at_ms=None, collected_at_ms=None)
            output = self._replace_seat(seats, reset)
            if replacement is None:
                return output, {"failed": asdict(reset), "replacement": None}, {}
            moved = replace(replacement, state=target, uid=failed.uid,
                            uname=failed.uname, reserved_at_ms=now_ms,
                            released_at_ms=now_ms if target == "released" else None)
            output = self._replace_seat(output, moved)
            return output, {"failed": asdict(reset), "replacement": asdict(moved)}, {}

        return self._apply(operation="replace_failed", arguments=args,
                           command_id=command_id, expected_revision=expected_revision,
                           mutate=mutate)

    def grow(
        self,
        *,
        additional_main: int = 0,
        additional_spares: int = 1,
        teacher_approved: bool,
        command_id: str,
        expected_revision: int,
    ) -> TransitionResult:
        """Append capacity without changing any existing slot or assignment."""
        mains = _validate_int("additional_main", additional_main)
        spares = _validate_int("additional_spares", additional_spares)
        if not teacher_approved:
            raise TeacherApprovalRequiredError("teacher approval is required to grow")
        if mains + spares == 0:
            raise PoolConfigurationError("grow must add at least one seat")
        if self.spare_count + spares > self.max_participants + mains:
            raise PoolConfigurationError(
                "resulting spare_count cannot exceed max_participants"
            )
        args = {"additional_main": mains, "additional_spares": spares,
                "teacher_approved": True}

        def mutate(seats):
            added = []
            next_slot = max((seat.slot_no for seat in seats), default=0) + 1
            for role, count in (("primary", mains), ("spare", spares)):
                for _ in range(count):
                    seat = Seat(next_slot, f"seat-{next_slot:03d}", role)
                    seats.append(seat)
                    added.append(asdict(seat))
                    next_slot += 1
            return seats, {"added": added}, {
                "max_participants": self.max_participants + mains,
                "spare_count": self.spare_count + spares,
            }

        return self._apply(operation="grow", arguments=args, command_id=command_id,
                           expected_revision=expected_revision, mutate=mutate)

    def freeze(
        self, *, now_ms: int, command_id: str, expected_revision: int
    ) -> TransitionResult:
        args = {"now_ms": now_ms}

        def mutate(seats):
            changed, output = [], []
            for seat in seats:
                if seat.uid is not None and seat.state in {"reserved", "released"}:
                    seat = replace(seat, state="frozen", frozen_at_ms=now_ms)
                    changed.append(asdict(seat))
                output.append(seat)
            return output, {"frozen": changed}, {}

        return self._apply(operation="freeze", arguments=args, command_id=command_id,
                           expected_revision=expected_revision, mutate=mutate)

    def collect(
        self, *, now_ms: int, command_id: str, expected_revision: int
    ) -> TransitionResult:
        args = {"now_ms": now_ms}

        def mutate(seats):
            changed, output = [], []
            for seat in seats:
                if seat.state == "frozen":
                    seat = replace(seat, state="collected", collected_at_ms=now_ms)
                    changed.append(asdict(seat))
                output.append(seat)
            return output, {"collected": changed}, {}

        return self._apply(operation="collect", arguments=args, command_id=command_id,
                           expected_revision=expected_revision, mutate=mutate)
