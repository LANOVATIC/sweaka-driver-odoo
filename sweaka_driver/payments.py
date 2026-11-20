"""Contract payment → Odoo inbound account.payment from the driver (T9.6.6)."""
from datetime import timezone

from odoo import fields

from .lib.sweaka_connect_core import Problem
from .lib.sweaka_connect_core.feed import iso
from .sales import _money_in, _when

METHODS = ("cash", "card", "bank", "other")


class PaymentService:
    def __init__(self, wms):
        self.wms, self.env = wms, wms.env

    def journal(self, company, method):
        Journal = self.env["account.journal"].sudo()
        kinds = ("bank", "cash") if method in ("card", "bank") else ("cash", "bank")
        for kind in kinds:
            journal = Journal.search([("type", "=", kind), ("company_id", "=", company.id)], limit=1)
            if journal:
                return journal
        raise Problem(503, "not_configured", detail="no cash/bank journal for driver payments")

    def create(self, driver, body):
        user = self.wms._user(driver)
        self.env = self.env(user=user.id, su=True)
        company, cur = user.company_id, user.company_id.currency_id
        Partner = self.env["res.partner"].with_context(active_test=False)
        if ("customer_id" in body) == ("customer_client_id" in body):
            raise Problem.field("customer_id")
        if "customer_id" in body:  # inactive customer accepted (collecting old debt)
            cid = str(body["customer_id"])
            partner, field = (Partner.browse(int(cid)).exists() if cid.isdigit() else Partner), "customer_id"
        else:
            partner = Partner.search([("sweaka_client_id", "=", str(body["customer_client_id"]))], limit=1)
            field = "customer_client_id"
        if not partner or partner.customer_rank <= 0:
            raise Problem.field(field)
        amount = _money_in(body.get("amount"), "amount", cur, minimum=1)
        method = body.get("method")
        if method not in METHODS:
            raise Problem.field("method")
        paid_at = _when(body.get("paid_at"), "paid_at")
        note = body.get("note")
        if note is not None and not (isinstance(note, str) and len(note) <= 1000):
            raise Problem.field("note")
        delivery = self.env["stock.picking"]
        if body.get("delivery_id") is not None:  # COD (T9.1.4h.4): the driver's own delivery
            did = str(body["delivery_id"])
            delivery = delivery.browse(int(did)).exists() if did.isdigit() else delivery
            if not delivery or delivery.sweaka_driver_id.id != user.id:
                raise Problem.field("delivery_id")
        payment = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": partner.id, "amount": amount,
            "currency_id": cur.id, "journal_id": self.journal(company, method).id, "date": paid_at.date(),
            ("memo" if "memo" in self.env["account.payment"]._fields else "ref"): "Sweaka " + body["client_id"][:8],
            "sweaka_client_id": body["client_id"],
            "sweaka_driver_id": user.id, "sweaka_delivery_id": delivery.id or False, "sweaka_method": method, "sweaka_paid_at": paid_at, "sweaka_note": note})
        payment.action_post()
        partner.sudo().write({"sweaka_touched_at": fields.Datetime.now()})
        return self.payment_data(payment)

    def payment_data(self, payment):
        cur = payment.currency_id
        to_utc = lambda dt: iso(dt.replace(tzinfo=timezone.utc)) if dt else None  # noqa: E731
        cancelled = payment.state == "canceled" or payment.state == "cancel"
        return {"id": str(payment.id), "client_id": payment.sweaka_client_id or None, "number": payment.name or str(payment.id),
                "customer_id": str(payment.partner_id.id), "status": "cancelled" if cancelled else "completed",
                "amount": {"amount_minor": int(round(payment.amount * 10 ** cur.decimal_places)), "currency": cur.name},
                "method": payment.sweaka_method or "other", "location": None, "location_captured_at": None,
                "note": payment.sweaka_note or None,
                "delivery_id": str(payment.sweaka_delivery_id.id) if payment.sweaka_delivery_id else None, "paid_at": to_utc(payment.sweaka_paid_at),
                "cancel_reason": None, "cancelled_at": to_utc(payment.write_date) if cancelled else None,
                "created_at": to_utc(payment.create_date), "updated_at": to_utc(payment.write_date)}
