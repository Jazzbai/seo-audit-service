"""Atomic per-site budget reservations with idempotent settlement."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .models import BudgetAccount, CostReservation, utcnow


_MAX_CENTS = (1 << 63) - 1
_MAX_MONTHLY_BUDGET_CENTS = 30_000


def _validate_cents(value: Any, name: str) -> int:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a known non-negative integer")
    if value < 0 or value > _MAX_CENTS:
        raise ValueError(f"{name} must be a known non-negative integer")
    return value


def _validate_operation_key(operation_key: str) -> str:
    if not isinstance(operation_key, str) or not operation_key.strip():
        raise ValueError("operation_key is required")
    return operation_key.strip()


def _advisory_key(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % ((1 << 63) - 1)


def _begin_atomic(db: Session, lock_value: str | None = None) -> None:
    """Start a write-serialized transaction when a fresh session is supplied."""

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        # An ORM read commonly opens the session transaction before this
        # helper is reached.  Advisory locks are still valid inside that
        # transaction and must not be skipped, otherwise the first monthly
        # account can be created concurrently by two callers.
        value = lock_value or "forgeseo-budget-global"
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_key(value)},
        )
        return
    if dialect == "sqlite":
        if db.in_transaction():
            # A normal read may have already opened a deferred SQLite
            # transaction.  A no-op write upgrades it to a writer lock before
            # the account is read; a competing reader receives a bounded lock
            # retry in reserve().
            if lock_value:
                db.execute(
                    text(
                        "UPDATE budget_accounts "
                        "SET reserved_cents = reserved_cents "
                        "WHERE site_id = :site_id AND period = :period"
                    ),
                    {"site_id": lock_value, "period": _period()},
                )
            return
        # SQLite has no row-level locks. BEGIN IMMEDIATE makes the read/check/
        # increment/insert sequence one atomic writer transaction.
        db.execute(text("BEGIN IMMEDIATE"))
        return
    if db.in_transaction():
        return
    else:
        db.begin()


def _period() -> str:
    return utcnow().strftime("%Y-%m")


def _account_for_update(db: Session, site_id: str, period: str, limit_cents: int) -> BudgetAccount:
    account = db.scalar(
        select(BudgetAccount)
        .where(BudgetAccount.site_id == site_id, BudgetAccount.period == period)
        .with_for_update()
    )
    if account is None:
        account = BudgetAccount(
            site_id=site_id,
            period=period,
            limit_cents=limit_cents,
            reserved_cents=0,
            spent_cents=0,
        )
        db.add(account)
        db.flush()
    return account


def reserve(
    db: Session,
    site_id: str,
    operation_key: str,
    estimated_cents: int,
    limit_cents: int = 30000,
) -> CostReservation:
    """Reserve budget exactly once for an operation.

    A repeated operation key returns its original reservation and never charges
    the account a second time. A known cost that cannot fit in the account is
    rejected before a reservation is created.
    """

    if not isinstance(site_id, str) or not site_id.strip():
        raise ValueError("site_id is required")
    operation_key = _validate_operation_key(operation_key)
    estimated_cents = _validate_cents(estimated_cents, "estimated_cents")
    limit_cents = _validate_cents(limit_cents, "limit_cents")
    if limit_cents > _MAX_MONTHLY_BUDGET_CENTS:
        raise ValueError("limit_cents may not exceed the $300/site/month ceiling")

    for attempt in range(2):
        try:
            _begin_atomic(db, site_id)
            existing = db.scalar(
                select(CostReservation)
                .where(CostReservation.operation_key == operation_key)
                .with_for_update()
            )
            if existing is not None:
                if existing.site_id != site_id or existing.estimated_cents != estimated_cents:
                    raise ValueError("operation_key already belongs to a different cost")
                db.commit()
                return existing

            account = _account_for_update(db, site_id, _period(), limit_cents)
            # The policy limit is versioned and may be lowered while the current
            # monthly account already has reservations.  Keep the account aligned
            # with the limit authorized for this operation; otherwise a later
            # reservation would continue using the higher limit from the first
            # operation of the month.
            account.limit_cents = limit_cents
            available_total = account.spent_cents + account.reserved_cents + estimated_cents
            # A zero-cost operation does not consume budget and remains usable for
            # local bookkeeping even after a paid operation has overflowed. Any
            # positive future spend is still denied while spent_cents exceeds the
            # limit.
            if estimated_cents > 0 and available_total > account.limit_cents:
                raise ValueError("monthly budget exceeded")

            account.reserved_cents += estimated_cents
            reservation = CostReservation(
                site_id=site_id,
                account_id=account.id,
                operation_key=operation_key,
                estimated_cents=estimated_cents,
                status="reserved",
            )
            db.add(reservation)
            db.flush()
            db.commit()
            return reservation
        except ValueError:
            db.rollback()
            raise
        except IntegrityError:
            # A concurrent request may have won either the globally unique
            # operation key or the monthly account's unique site/period key.
            # Recover the idempotent record, or retry the account race once
            # from a fresh transaction so it is evaluated against the winner.
            db.rollback()
            existing = db.scalar(
                select(CostReservation).where(CostReservation.operation_key == operation_key)
            )
            if existing is not None and existing.site_id == site_id and existing.estimated_cents == estimated_cents:
                return existing
            if attempt == 0:
                db.rollback()
                continue
            raise
        except OperationalError as exc:
            # Two SQLite readers cannot both upgrade to writers.  Retry a
            # single lock conflict after rollback; other operational failures
            # remain visible to the caller.
            db.rollback()
            sqlite_lock = (
                db.get_bind().dialect.name == "sqlite"
                and "locked" in str(exc).casefold()
            )
            if (
                attempt == 0
                and sqlite_lock
            ):
                continue
            raise


def settle(db: Session, reservation_id: str, actual_cents: int) -> CostReservation:
    """Settle a reservation, retaining overflow as real spent usage."""

    actual_cents = _validate_cents(actual_cents, "actual_cents")
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise ValueError("reservation_id is required")

    try:
        _begin_atomic(db)
        reservation = db.scalar(
            select(CostReservation)
            .where(CostReservation.id == reservation_id)
            .with_for_update()
        )
        if reservation is None:
            raise ValueError("cost reservation not found")

        if reservation.status == "settled":
            if reservation.actual_cents != actual_cents:
                raise ValueError("reservation is already settled with a different cost")
            db.commit()
            return reservation
        if reservation.status == "released":
            raise ValueError("released reservation cannot be settled")
        if reservation.status != "reserved":
            raise ValueError("reservation has an invalid status")

        account = db.scalar(
            select(BudgetAccount)
            .where(BudgetAccount.id == reservation.account_id)
            .with_for_update()
        )
        if account is None or account.reserved_cents < reservation.estimated_cents:
            raise ValueError("budget reservation accounting is inconsistent")

        account.reserved_cents -= reservation.estimated_cents
        # Do not cap or roll back actual usage. Over-budget actual cost remains
        # visible and prevents subsequent spending.
        account.spent_cents += actual_cents
        reservation.actual_cents = actual_cents
        reservation.status = "settled"
        db.flush()
        db.commit()
        return reservation
    except ValueError:
        db.rollback()
        raise


def release(db: Session, reservation_id: str) -> CostReservation:
    """Release only the outstanding hold; settled usage is never erased."""

    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise ValueError("reservation_id is required")

    try:
        _begin_atomic(db)
        reservation = db.scalar(
            select(CostReservation)
            .where(CostReservation.id == reservation_id)
            .with_for_update()
        )
        if reservation is None:
            raise ValueError("cost reservation not found")

        if reservation.status == "released":
            db.commit()
            return reservation
        if reservation.status == "settled":
            db.commit()
            return reservation
        if reservation.status != "reserved":
            raise ValueError("reservation has an invalid status")

        account = db.scalar(
            select(BudgetAccount)
            .where(BudgetAccount.id == reservation.account_id)
            .with_for_update()
        )
        if account is None or account.reserved_cents < reservation.estimated_cents:
            raise ValueError("budget reservation accounting is inconsistent")

        account.reserved_cents -= reservation.estimated_cents
        reservation.status = "released"
        db.flush()
        db.commit()
        return reservation
    except ValueError:
        db.rollback()
        raise


__all__ = ["release", "reserve", "settle"]
