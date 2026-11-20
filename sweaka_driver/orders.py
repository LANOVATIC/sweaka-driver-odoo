"""Contract orders (cap orders, T9.6.11): sale.order kind 'order'. requested = quotation (draft/sent),
approved = confirmed, delivered = all pickings done, cancelled = cancel. Driver may edit/cancel only while requested."""
from datetime import datetime, timezone

from .lib.sweaka_connect_core import Problem
from .lib.sweaka_connect_core.feed import iso
from .sales import SaleService, _money_in, _when, apply_discount


def _status(order):
    if order.state == "cancel":
        return "cancelled"
    if order.state in ("draft", "sent"):
        return "requested"
    pickings = order.picking_ids.filtered(lambda p: p.state != "cancel")
    return "delivered" if pickings and all(p.state == "done" for p in pickings) else "approved"


class OrderService:
    def __init__(self, wms):
        self.wms, self.env = wms, wms.env

    def _as_driver(self, driver):
        user = self.wms._user(driver)
        self.env = self.env(user=user.id, su=True)
        return user

    def _discount(self, body, cur):
        return _money_in(body["discount"], "discount", cur) if body.get("discount") not in (None, {}) else 0.0

    def create(self, driver, body):
        user = self._as_driver(driver)
        company, cur = user.company_id, user.company_id.currency_id
        sales = SaleService(self.wms)
        sales.env = self.env
        partner = sales.customer(body, company)
        lines = sales.priced_lines(body.get("lines"), partner, company)
        discount = self._discount(body, cur)
        planned = body.get("planned_delivery_at")
        planned = _when(planned, "planned_delivery_at") if planned is not None else False
        note = body.get("note")
        if note is not None and not (isinstance(note, str) and len(note) <= 1000):
            raise Problem.field("note")
        loc = body.get("location") if isinstance(body.get("location"), dict) else {}
        order = self.env["sale.order"].with_company(company).create({
            "partner_id": partner.id, "company_id": company.id, "user_id": user.id, "order_line": lines,
            "commitment_date": planned, "sweaka_client_id": body["client_id"], "sweaka_driver_id": user.id,
            "sweaka_kind": "order", "sweaka_note": note or False,
            "sweaka_latitude": loc.get("lat") or 0.0, "sweaka_longitude": loc.get("lon") or 0.0})
        apply_discount(order, discount, company)
        return self.order_data(order)

    def _own(self, driver, order_id):
        Order = self.env["sale.order"]
        order = Order.browse(int(order_id)).exists() if str(order_id).isdigit() else Order
        if not order or order.sweaka_kind != "order" or order.sweaka_driver_id.id != int(driver.id):
            raise Problem(404, "not_found")
        return order

    def _gate(self, order, body):
        if not isinstance(body, dict):
            raise Problem.field("body")
        if _status(order) != "requested":
            raise Problem(409, "not_editable")
        base = body.get("base_updated_at")
        try:
            base_dt = datetime.fromisoformat(str(base).replace("Z", "+00:00"))
        except ValueError:
            raise Problem.field("base_updated_at") from None
        if base_dt.astimezone(timezone.utc).replace(tzinfo=None) != order.write_date:
            raise Problem(409, "stale_write", current=self.order_data(order))

    def edit(self, driver, order_id, body):
        user = self._as_driver(driver)
        company = user.company_id
        order = self._own(driver, order_id)
        self._gate(order, body)
        if not all(k in body for k in ("lines", "planned_delivery_at", "note")):
            raise Problem.field("body")
        sales = SaleService(self.wms)
        sales.env = self.env
        lines = sales.priced_lines(body["lines"], order.partner_id, company)
        discount = self._discount(body, company.currency_id)  # full replacement: missing = no discount
        planned = body["planned_delivery_at"]
        order.write({"order_line": [(5, 0, 0)] + lines,
                     "commitment_date": _when(planned, "planned_delivery_at") if planned is not None else False,
                     "sweaka_note": body["note"] or False})
        apply_discount(order, discount, company)
        return self.order_data(order)

    def cancel(self, driver, order_id, body):
        self._as_driver(driver)
        order = self._own(driver, order_id)
        if order.state == "cancel":
            return self.order_data(order)  # already cancelled: base not checked
        self._gate(order, body)
        order.write({"sweaka_cancel_reason": (body.get("reason") or "")[:255] or False})
        order._action_cancel() if hasattr(order, "_action_cancel") else order.action_cancel()
        return self.order_data(order)

    def order_data(self, order):
        base = SaleService(self.wms).sale_data(order)
        status = _status(order)
        to_utc = lambda dt: iso(dt.replace(tzinfo=timezone.utc)) if dt else None  # noqa: E731
        done = order.picking_ids.filtered(lambda p: p.state == "done").mapped("date_done")
        return {"id": base["id"], "client_id": base["client_id"], "number": base["number"],
                "customer_id": base["customer_id"], "status": status,
                "planned_delivery_at": to_utc(order.commitment_date), "lines": base["lines"],
                "subtotal": base["subtotal"], "tax_total": base["tax_total"], "discount_total": base["discount_total"],
                "total": base["total"], "location": base["location"], "location_captured_at": None,
                "note": order.sweaka_note or None, "cancel_reason": order.sweaka_cancel_reason or None,
                "delivered_at": to_utc(max(done)) if status == "delivered" and done else None,
                "cancelled_at": to_utc(order.write_date) if status == "cancelled" else None,
                "created_at": base["created_at"], "updated_at": to_utc(order.write_date)}
