"""The settlement journal. Source statements are not allowed to change balances."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value: str | int | float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Account:
    owner: str
    role: str
    opening: Decimal


@dataclass(frozen=True)
class Leg:
    source: str
    destination: str
    amount: Decimal
    purpose: str


@dataclass(frozen=True)
class State:
    opening: Decimal
    unit: str
    floor: Decimal | None = ZERO
    ceiling: Decimal | None = None


@dataclass(frozen=True)
class Change:
    key: str
    delta: Decimal


@dataclass(frozen=True)
class Business:
    kind: str
    item: str
    category: str
    expense_class: str
    cadence: str
    reference: str = ""


@dataclass(frozen=True)
class Event:
    id: str
    at: datetime
    business: Business
    amount: Decimal
    payer: str
    payee: str
    initiator: str
    status: str
    legs: tuple[Leg, ...] = ()
    changes: tuple[Change, ...] = ()
    original: str = ""
    route: tuple[str, ...] = ()
    credit: str = ""
    failure: str = ""


class Journal:
    def __init__(self):
        self.actors: dict[str, tuple[str, str]] = {}
        self.accounts: dict[str, Account] = {}
        self.cash: dict[str, Decimal] = {}
        self.states: dict[str, State] = {}
        self.values: dict[str, Decimal] = {}
        self.events: list[Event] = []
        self.by_id: dict[str, Event] = {}

    def actor(self, key: str, name: str, role: str) -> str:
        if self.actors.setdefault(key, (name, role)) != (name, role):
            raise ValueError(f"inconsistent actor: {key}")
        return key

    def account(self, key: str, owner: str, role: str, opening=ZERO) -> str:
        if owner not in self.actors or opening < 0 or opening != money(opening):
            raise ValueError(f"invalid cash account: {key}")
        spec = Account(owner, role, opening)
        if self.accounts.setdefault(key, spec) != spec:
            raise ValueError(f"inconsistent cash account: {key}")
        self.cash.setdefault(key, opening)
        return key

    def state(self, key: str, opening=ZERO, *, unit="CNY", floor=ZERO, ceiling=None) -> str:
        spec = State(opening, unit, floor, ceiling)
        if ((floor is not None and opening < floor)
                or (ceiling is not None and opening > ceiling)
                or self.states.setdefault(key, spec) != spec):
            raise ValueError(f"invalid state: {key}")
        self.values.setdefault(key, opening)
        return key

    @staticmethod
    def _limit(spec: State, value: Decimal) -> bool:
        return ((spec.floor is None or value >= spec.floor)
                and (spec.ceiling is None or value <= spec.ceiling))

    def check(self, legs: tuple[Leg, ...], changes: tuple[Change, ...]):
        cash, values = {}, {}
        for leg in legs:
            if leg.source == leg.destination or leg.amount <= 0 or leg.amount != money(leg.amount):
                raise ValueError(f"invalid cash leg: {leg}")
            available = cash.get(leg.source, self.cash[leg.source])
            if available < leg.amount:
                return {}, {}, f"insufficient_cash:{leg.source}"
            cash[leg.source] = available - leg.amount
            cash[leg.destination] = cash.get(leg.destination, self.cash[leg.destination]) + leg.amount
        for change in changes:
            values[change.key] = values.get(change.key, self.values[change.key]) + change.delta
        for key, value in values.items():
            if not self._limit(self.states[key], value):
                return {}, {}, f"state_limit:{key}"
        return cash, values, ""

    def record(self, at: datetime, business: Business, amount: Decimal, *,
               payer: str, payee: str, initiator: str | None = None,
               legs: tuple[Leg, ...] = (), changes: tuple[Change, ...] = (),
               original: str = "", route: tuple[str, ...] = (), credit: str = "",
               status: str = "settled", failure: str = "") -> Event:
        initiator = initiator or payer
        if any(a not in self.actors for a in (payer, payee, initiator)):
            raise ValueError("unregistered actor")
        if self.events and at < self.events[-1].at:
            raise ValueError(f"out-of-order event: {business.item} {at}")
        if status == "settled":
            cash, values, failure = self.check(legs, changes)
            if failure:
                status = "unpaid" if business.kind in {"contract", "payroll", "loan"} else "failed"
            else:
                self.cash.update(cash)
                self.values.update(values)
        if status != "settled":
            legs, changes = (), ()
        event = Event(f"EV{len(self.events) + 1:06d}", at, business, amount,
                      payer, payee, initiator, status, legs, changes, original, route, credit, failure)
        self.events.append(event)
        self.by_id[event.id] = event
        return event
