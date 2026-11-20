"""Adapter ports (T9.5.1). A WMS plugin / Bridge adapter implements WmsPort (+ StatePort for its storage).

Records are plain dicts already shaped like the contract read schemas (Product, Customer, Sale, ...): the adapter
maps its WMS model to contract JSON; the core never inspects WMS objects.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol

FEED_RANK = {"product": 1, "customer": 2, "van_stock": 3, "van_load": 4, "order": 5, "sale": 6, "payment": 7,
             "return": 8, "stock_loss": 9, "cash_handover": 10, "driver_cash": 11, "write_rejected": 12}


@dataclass(frozen=True)
class Driver:
    id: str  # contract Me.driver.id
    tenant_id: str
    name: str
    extra: dict = field(default_factory=dict, compare=False, hash=False)  # adapter-private (WMS user id ...)


@dataclass(frozen=True, order=True)
class ChangeKey:
    """Total order of the change feed: (changed_at UTC, type rank, id)."""

    changed_at: datetime
    rank: int
    id: str


@dataclass
class Change:
    type: str
    id: str
    changed_at: datetime
    data: Optional[dict] = None  # None = tombstone (op delete)
    client_id: Optional[str] = None

    @property
    def key(self):
        return ChangeKey(self.changed_at, FEED_RANK.get(self.type, 99), self.id)


class WmsPort(Protocol):
    def capabilities(self) -> dict: ...

    def action(self, driver: Driver, path: str, method: str, ids: tuple, body: Any) -> dict:
        """Non-create mutating contract op (location update, order edit/cancel, van-load validate, day close)."""
        ...

    def authenticate(self, username: str, password: str) -> Optional[Driver]:
        """None = wrong credentials; raise Problem(403, 'forbidden') for a non-driver / blocked tenant."""
        ...

    def driver(self, driver_id: str) -> Optional[Driver]: ...

    def profile(self, driver: Driver) -> dict:
        """Contract Me body: {driver, tenant, settings}."""
        ...

    def changes(self, driver: Driver, after: Optional[ChangeKey], limit: int, snapshot: bool) -> list:
        """Up to limit+1 Change rows with key > after, ordered by key. snapshot=True: visible records only
        (no tombstones). Types of capabilities switched off must not appear."""
        ...

    def find_by_client_id(self, driver: Driver, resource: str, client_id: str) -> Optional[tuple]:
        """→ (record dict, visible_to_driver: bool) for a known client_id, else None (Client ids rule)."""
        ...

    def create(self, driver: Driver, resource: str, body: Any) -> dict:
        """Validate + apply one create (resource = 'customers' | 'sales' | 'payments' | ...); raise Problem on
        a rule violation (400 with fields). Effects in one transaction. → created contract record."""
        ...


class StatePort(Protocol):
    """Tokens + idempotency outcomes (core-owned state the WMS has no table for)."""

    def issue_token(self, driver: Driver, device: dict) -> str: ...

    def token_driver(self, token: str) -> Optional[str]:
        """→ driver id of a live token, else None."""
        ...

    def revoke_token(self, token: str) -> None: ...

    def idem_get(self, driver_id: str, key: str) -> Optional[tuple]:
        """→ (fingerprint, status, body) or (fingerprint, None, None) while in progress."""
        ...

    def idem_begin(self, driver_id: str, key: str, fingerprint: str) -> bool:
        """Reserve the key; False if someone holds it already."""
        ...

    def idem_finish(self, driver_id: str, key: str, status: Optional[int], body: Any) -> None:
        """status None = drop the reservation (retryable outcome, not stored)."""
        ...
