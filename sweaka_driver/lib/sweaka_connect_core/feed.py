"""/changes cursors + paging over an adapter's ordered change stream (contract "Changes feed").

Cursor = opaque base64url JSON {m: s|d, t, r, i}: mode s = snapshot (visible records only) until done, then d.
Overlap window: a caught-up page never moves the cursor past now - LAG, so a WMS transaction committing after
its timestamp is re-scanned (duplicates allowed, never a gap).
"""
import base64
import json
from datetime import datetime, timedelta, timezone

from .errors import Problem
from .ports import ChangeKey

LAG = timedelta(seconds=2)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def encode(mode, key):
    raw = json.dumps({"m": mode, "t": key.changed_at.isoformat(), "r": key.rank, "i": key.id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode(cursor):
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        key = ChangeKey(datetime.fromisoformat(raw["t"]), int(raw["r"]), str(raw["i"]))
        if raw["m"] not in ("s", "d") or key.changed_at.tzinfo is None:
            raise ValueError(cursor)
        return raw["m"], key
    except (ValueError, KeyError, TypeError):
        raise Problem(400, "invalid_cursor") from None


def iso(ts):
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def page(wms, driver, since, limit, now=None):
    mode, key = decode(since) if since else ("s", None)
    expired = getattr(wms, "cursor_expired", None)
    if key is not None and expired is not None and expired(driver, key):
        raise Problem(410, "resync_required")
    rows = sorted(wms.changes(driver, key, limit, snapshot=(mode == "s")), key=lambda c: c.key)
    served, has_more = rows[:limit], len(rows) > limit
    items = []
    for c in served:
        item = {"type": c.type, "id": c.id, "client_id": c.client_id, "changed_at": iso(c.changed_at)}
        if c.data is None:
            item["op"] = "delete"
        else:
            item["op"], item["data"] = "upsert", c.data
        items.append(item)
    horizon = ChangeKey((now or datetime.now(timezone.utc)) - LAG, 0, "")
    last = served[-1].key if served else (key or horizon)  # empty first page: start from now, never epoch
    if not has_more:
        last = min(last, horizon)
        if key is not None and last < key:
            last = key
    return {"items": items, "next_cursor": encode(mode if has_more else "d", last), "has_more": has_more}
