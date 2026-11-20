"""MemoryWms: a WMS held in memory, with the contract's van-sale rules (pricing, debt, discount cap, client_total,
van stock, balances, driver cash). Used by the core's fake adapter (tests) and the Bridge CSV adapter (T9.8.10).

Seed = contract-shaped records: object with USERNAME, PASSWORD, PRODUCTS, CUSTOMERS, VAN_STOCK.
on_create(kind, record) is called after each create (export hook: CSV out, outbox, ...).
Implements caps customers_create, van_sales, payments, van_stock; all other ops are capability_disabled.
"""
import copy
import itertools
import uuid
from datetime import datetime, timedelta, timezone

from .errors import Problem
from .feed import iso
from .ports import Change, Driver

CAPS = ("customers_create", "customers_update_location", "orders", "deliveries", "van_sales", "payments",
        "returns", "stock_loss", "van_stock", "stock_validation", "day_close", "gps_tracking")
ON = {"customers_create", "van_sales", "payments", "van_stock"}
METHODS = ("cash", "card", "bank", "other")


def _half_up(num, den):
    return (2 * num + den) // (2 * den)


class MemoryWms:
    def __init__(self, seed, currency="DZD", debt_allowed=True, max_discount_percent=10, on_create=None,
                 cities=("city-16",), default_zone=("z-7", "Bab Ezzouar")):
        self.seed, self.currency, self.on_create = seed, currency, on_create
        self.cities, self.default_zone = set(cities), default_zone
        self.settings = {"currency": currency, "default_delivery_days": 2, "can_sign_out": True,
                         "hide_today_money": False, "gps_required": False, "debt_allowed": debt_allowed,
                         "max_discount_percent": max_discount_percent}
        self.me = Driver(id="57", tenant_id="tenant-core", name="Karim Benali")
        self._clock = itertools.count()
        self._t0 = datetime.now(timezone.utc)
        self.records = {}  # (type, id) → [changed_at, data, client_id]
        for p in seed.PRODUCTS:
            self._put("product", p["id"], p)
        for c in seed.CUSTOMERS:
            self._put("customer", c["id"], c)
        for v in seed.VAN_STOCK:
            self._put("van_stock", v["id"], v)
        self._put("driver_cash", self.me.id, {"id": self.me.id, "cash_on_hand": self._m(0),
                                               "customer_credit_total": self._m(self._credit()),
                                               "updated_at": iso(self._t0)})
        self.by_client_id = {}  # (resource, client_id) → (type, id)

    # ---- helpers ---------------------------------------------------------------------------------------------
    def _now(self):
        """Strictly increasing timestamps (feed keys must not collide)."""
        return max(datetime.now(timezone.utc), self._t0 + timedelta(microseconds=next(self._clock)))

    def _m(self, amount):
        return {"amount_minor": amount, "currency": self.currency}

    def _put(self, rtype, rid, data, client_id=None):
        at = self._now()
        data = copy.deepcopy(data)
        if "updated_at" in data:
            data["updated_at"] = iso(at)
        self.records[(rtype, rid)] = [at, data, client_id or data.get("client_id")]
        return data

    def _get(self, rtype, rid):
        entry = self.records.get((rtype, rid))
        return entry[1] if entry else None

    def _all(self, rtype):
        return [e[1] for (t, _), e in self.records.items() if t == rtype]

    def _credit(self):
        return sum(c["balance"]["amount_minor"] for c in self._all("customer"))

    # ---- WmsPort ----------------------------------------------------------------------------------------------
    def capabilities(self):
        return {c: c in ON for c in CAPS}

    def authenticate(self, username, password):
        return self.me if (username, password) == (self.seed.USERNAME, self.seed.PASSWORD) else None

    def driver(self, driver_id):
        return self.me if driver_id == self.me.id else None

    def profile(self, driver):
        return {"driver": {"id": driver.id, "name": driver.name, "phone": None, "avatar_url": None},
                "tenant": {"id": driver.tenant_id, "name": "Dépôt Core", "address": None, "phone": None,
                           "email": None, "logo_url": None},
                "settings": dict(self.settings)}

    def changes(self, driver, after, limit, snapshot):
        rows = [Change(t, rid, e[0], e[1], e[2]) for (t, rid), e in self.records.items()]
        rows = [c for c in rows if after is None or c.key > after]
        return sorted(rows, key=lambda c: c.key)[: limit + 1]

    def find_by_client_id(self, driver, resource, client_id):
        ref = self.by_client_id.get((resource, client_id))
        return (self._get(*ref), True) if ref else None

    def action(self, driver, path, method, ids, body):
        raise Problem(404, "capability_disabled")

    def create(self, driver, resource, body):
        handler = {"customers": self._customer, "sales": self._sale, "payments": self._payment}.get(resource)
        if handler is None:
            raise Problem(404, "capability_disabled")
        rtype, record = handler(body)
        self.by_client_id[(resource, body["client_id"])] = (rtype, record["id"])
        if self.on_create:
            self.on_create(rtype, record)
        return record

    # ---- domain -----------------------------------------------------------------------------------------------
    def _customer(self, body):
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 256:
            raise Problem.field("name")
        if body.get("city_id") is not None and body.get("city_id") not in self.cities:
            raise Problem.field("city_id")
        known_zones = {z["id"] for c in self._all("customer") for z in c.get("zones", [])} | {self.default_zone[0]}
        zones = body.get("zone_ids") or [self.default_zone[0]]
        bad = [i for i, z in enumerate(zones) if z not in known_zones]
        if bad:
            raise Problem.field(f"zone_ids[{bad[0]}]")
        record = {"id": str(uuid.uuid4()), "client_id": body["client_id"], "name": name,
                  "phone": body.get("phone"), "address": body.get("address"),
                  "city": {"id": body["city_id"], "name": body["city_id"]} if body.get("city_id") else None,
                  "location": body.get("location"), "category": None,
                  "zones": [{"id": z, "name": z if z != self.default_zone[0] else self.default_zone[1]} for z in zones],
                  "active": True, "balance": self._m(0), "credit_limit": None, "updated_at": ""}
        return "customer", self._put("customer", record["id"], record)

    def _resolve_customer(self, body, active_only=True):
        if ("customer_id" in body) == ("customer_client_id" in body):
            raise Problem.field("customer_id")
        if "customer_id" in body:
            customer = self._get("customer", body["customer_id"])
            if customer is None or (active_only and not customer["active"]):
                raise Problem.field("customer_id")
            return customer
        ref = self.by_client_id.get(("customers", body["customer_client_id"]))
        if ref is None:
            raise Problem.field("customer_client_id")
        return self._get(*ref)

    def _money_in(self, value, field, minimum=0):
        if not isinstance(value, dict) or not isinstance(value.get("amount_minor"), int) or isinstance(
                value.get("amount_minor"), bool) or value["amount_minor"] < minimum:
            raise Problem.field(field)
        if value.get("currency") != self.currency:
            raise Problem.field(field + ".currency")
        return value["amount_minor"]

    def _price(self, product, customer, qty):
        cat = (customer.get("category") or {}).get("id")
        zones = {z["id"] for z in customer.get("zones", [])}
        best = None
        for t in product["prices"]:
            if not (t["min_qty"] <= qty and (t["max_qty"] is None or qty <= t["max_qty"])):
                continue
            if (t["customer_category_ids"] and cat not in t["customer_category_ids"]) or (
                    t["zone_ids"] and not zones & set(t["zone_ids"])):
                continue
            rank = (2 * bool(t["customer_category_ids"]) + bool(t["zone_ids"]), t["min_qty"],
                    -t["unit_price"]["amount_minor"])
            if best is None or rank > best[0]:
                best = (rank, t)
        return best and best[1]

    def _move_balance(self, customer, delta):
        customer = copy.deepcopy(customer)
        customer["balance"] = self._m(customer["balance"]["amount_minor"] + delta)
        self._put("customer", customer["id"], customer)

    def _move_cash(self, delta):
        cash = copy.deepcopy(self._get("driver_cash", self.me.id))
        cash["cash_on_hand"] = self._m(cash["cash_on_hand"]["amount_minor"] + delta)
        cash["customer_credit_total"] = self._m(self._credit())
        self._put("driver_cash", self.me.id, cash)

    def _sale(self, body):
        customer = self._resolve_customer(body)
        lines = body.get("lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= 500:
            raise Problem.field("lines")
        priced, seen, subtotal, tax_total = [], set(), 0, 0
        for i, line in enumerate(lines):
            product = self._get("product", line.get("product_id")) if isinstance(line, dict) else None
            qty = line.get("qty") if isinstance(line, dict) else None
            if not product or not product["active"] or product["id"] in seen:
                raise Problem.field(f"lines[{i}].product_id")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                raise Problem.field(f"lines[{i}].qty")
            tier = self._price(product, customer, qty)
            if tier is None:
                raise Problem.field(f"lines[{i}].product_id", "no_price")
            seen.add(product["id"])
            total = tier["unit_price"]["amount_minor"] * qty * product["pack_size"]
            tax = _half_up(total * int(product["tax_rate"]), 100)
            subtotal, tax_total = subtotal + total, tax_total + tax
            priced.append({"product_id": product["id"], "product_name": product["name"], "qty": qty,
                           "unit_price": dict(tier["unit_price"]), "tax_rate": product["tax_rate"],
                           "total": self._m(total), "tax": self._m(tax)})
        gross = subtotal + tax_total
        discount = 0
        if body.get("discount") is not None:
            discount = self._money_in(body["discount"], "discount")
            if discount * 100 > gross * self.settings["max_discount_percent"]:
                raise Problem.field("discount", "discount_too_high")
        total = gross - discount
        paid = self._money_in(body.get("paid"), "paid")
        if paid < total and not self.settings["debt_allowed"]:
            raise Problem.field("paid", "debt_not_allowed")
        client_total = None
        if body.get("client_total") is not None:
            self._money_in(body["client_total"], "client_total")
            client_total = body["client_total"]
        try:
            datetime.fromisoformat(str(body.get("sold_at")).replace("Z", "+00:00"))
        except ValueError:
            raise Problem.field("sold_at") from None
        record = {"id": str(uuid.uuid4()), "client_id": body["client_id"],
                  "number": f"BL-{len(self._all('sale')) + 1:06d}", "customer_id": customer["id"],
                  "status": "completed", "lines": priced, "subtotal": self._m(subtotal),
                  "tax_total": self._m(tax_total), "discount_total": self._m(discount), "total": self._m(total),
                  "paid": self._m(paid), "client_total": client_total, "location": body.get("location"),
                  "location_captured_at": body.get("location_captured_at") if body.get("location") else None,
                  "note": body.get("note"), "sold_at": body["sold_at"], "cancel_reason": None,
                  "cancelled_at": None, "created_at": iso(self._now()), "updated_at": ""}
        record = self._put("sale", record["id"], record)
        for line in priced:
            stock = next((s for s in self._all("van_stock") if s["product_id"] == line["product_id"]), None)
            if stock is not None:
                stock = dict(stock, qty=stock["qty"] - line["qty"])
                self._put("van_stock", stock["id"], stock)
        self._move_balance(customer, total - paid)
        self._move_cash(paid)
        return "sale", record

    def _payment(self, body):
        customer = self._resolve_customer(body, active_only=False)
        if body.get("delivery_id") is not None:  # no deliveries in this WMS (T9.1.4h.4)
            raise Problem.field("delivery_id")
        amount = self._money_in(body.get("amount"), "amount", minimum=1)
        if body.get("method") not in METHODS:
            raise Problem.field("method")
        try:
            datetime.fromisoformat(str(body.get("paid_at")).replace("Z", "+00:00"))
        except ValueError:
            raise Problem.field("paid_at") from None
        record = {"id": str(uuid.uuid4()), "client_id": body["client_id"],
                  "number": f"RC-{len(self._all('payment')) + 1:06d}", "customer_id": customer["id"],
                  "status": "completed", "amount": self._m(amount), "method": body["method"], "delivery_id": None,
                  "location": body.get("location"),
                  "location_captured_at": body.get("location_captured_at") if body.get("location") else None,
                  "note": body.get("note"), "paid_at": body["paid_at"], "cancel_reason": None,
                  "cancelled_at": None, "created_at": iso(self._now()), "updated_at": ""}
        record = self._put("payment", record["id"], record)
        self._move_balance(customer, -amount)
        self._move_cash(amount)
        return "payment", record
