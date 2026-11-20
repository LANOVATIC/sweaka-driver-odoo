"""Contract van sale → Odoo: sale.order (server-priced) → delivery from the driver's van → posted invoice →
cash payment for 'paid' (T9.6.5). One request = one transaction (Odoo rolls back on error)."""
from datetime import datetime, timezone

from odoo import fields

from .lib.sweaka_connect_core import Problem
from .lib.sweaka_connect_core.feed import iso


def _money_in(value, field, currency, minimum=0):
    if not isinstance(value, dict) or not isinstance(value.get("amount_minor"), int) or isinstance(
            value.get("amount_minor"), bool) or value["amount_minor"] < minimum:
        raise Problem.field(field)
    if value.get("currency") != currency.name:
        raise Problem.field(field + ".currency")
    return value["amount_minor"] / 10 ** currency.decimal_places


def _when(value, field):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise Problem.field(field) from None
    if dt.tzinfo is None:
        raise Problem.field(field)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class SaleService:
    def __init__(self, wms):
        self.wms, self.env = wms, wms.env

    def customer(self, body, company):
        Partner = self.env["res.partner"].sudo()
        if ("customer_id" in body) == ("customer_client_id" in body):
            raise Problem.field("customer_id")
        if "customer_id" in body:
            cid = str(body["customer_id"])
            partner = Partner.browse(int(cid)).exists() if cid.isdigit() else Partner
            field = "customer_id"
        else:
            partner = Partner.search([("sweaka_client_id", "=", str(body["customer_client_id"]))], limit=1)
            field = "customer_client_id"
        if not partner or not partner.active or partner.customer_rank <= 0:
            raise Problem.field(field)
        return partner

    def best_tier(self, product, partner, company, qty):
        tiers, _ = self.wms._tiers(product, company)
        pricelist = self.wms.partner_pricelist(partner, company)
        cat = str(pricelist.id) if pricelist else None
        zones = {str(t.id) for t in partner.category_id}
        best = None
        for t in tiers:
            if qty < t["min_qty"] or (t["max_qty"] is not None and qty > t["max_qty"]):
                continue
            if (t["customer_category_ids"] and cat not in t["customer_category_ids"]) or (
                    t["zone_ids"] and not zones & set(t["zone_ids"])):
                continue
            rank = (2 * bool(t["customer_category_ids"]) + bool(t["zone_ids"]), t["min_qty"],
                    -t["unit_price"]["amount_minor"])
            if best is None or rank > best[0]:
                best = (rank, t)
        return best and best[1]

    def create(self, driver, body):
        user = self.wms._user(driver)
        self.env = self.env(user=user.id, su=True)  # create_uid / defaults = the driver; rights = sudo
        company, cur = user.company_id, user.company_id.currency_id
        van = user.sweaka_van_location_id
        if not van:
            raise Problem(403, "forbidden", detail="driver has no van location")
        partner = self.customer(body, company)
        order_lines = self.priced_lines(body.get("lines"), partner, company)
        paid = _money_in(body.get("paid"), "paid", cur)
        return self._finish_sale(driver, body, user, company, cur, van, partner, order_lines, paid)

    def priced_lines(self, lines, partner, company):
        """[{product_id, qty}] → sale.order.line commands at the contract price (most specific tier)."""
        cur = company.currency_id
        if not isinstance(lines, list) or not 1 <= len(lines) <= 500:
            raise Problem.field("lines")
        Product, order_lines, seen = self.env["product.product"].sudo(), [], set()
        for i, line in enumerate(lines):
            pid = str(line.get("product_id")) if isinstance(line, dict) else ""
            product = Product.browse(int(pid)).exists() if pid.isdigit() else Product
            qty = line.get("qty") if isinstance(line, dict) else None
            if not product or not product.active or not product.sale_ok or product.id in seen:
                raise Problem.field(f"lines[{i}].product_id")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                raise Problem.field(f"lines[{i}].qty")
            tier = self.best_tier(product, partner, company, qty)
            if tier is None:
                raise Problem.field(f"lines[{i}].product_id", "no_price")
            seen.add(product.id)
            order_lines.append((0, 0, {"product_id": product.id, "product_uom_qty": qty,
                                       "price_unit": tier["unit_price"]["amount_minor"] / 10 ** cur.decimal_places}))
        return order_lines

    def _finish_sale(self, driver, body, user, company, cur, van, partner, order_lines, paid):
        discount = 0.0
        if body.get("discount") not in (None, {}):
            discount = _money_in(body["discount"], "discount", cur)
        client_total = None
        if body.get("client_total") is not None:
            client_total = _money_in(body["client_total"], "client_total", cur)
        sold_at = _when(body.get("sold_at"), "sold_at")
        loc = body.get("location") if isinstance(body.get("location"), dict) else {}
        order = self.env["sale.order"].sudo().with_company(company).create({
            "partner_id": partner.id, "company_id": company.id, "user_id": user.id, "order_line": order_lines,
            "sweaka_client_id": body["client_id"], "sweaka_driver_id": user.id, "sweaka_paid": paid,
            "sweaka_client_total": client_total or 0.0, "sweaka_sold_at": sold_at, "sweaka_note": body.get("note") or False,
            "sweaka_latitude": loc.get("lat") or 0.0, "sweaka_longitude": loc.get("lon") or 0.0, "sweaka_kind": "sale",
        })
        apply_discount(order, discount, company)
        if order.amount_total > paid + 1e-9 and not company.sweaka_debt_allowed:
            raise Problem.field("paid", "debt_not_allowed")
        order.action_confirm()
        self._deliver_from_van(order, van)
        invoice = order._create_invoices()
        invoice.action_post()
        if paid > 0:
            self._register_cash(invoice, paid, company)
        partner.write({"sweaka_touched_at": fields.Datetime.now()})  # balance moved → feed re-sends customer
        return self.sale_data(order)

    def _deliver_from_van(self, order, van):
        for picking in order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")):
            # reserved from the warehouse stock at confirm → its move lines stay there (Odoo 17) → unreserve first
            picking.do_unreserve()
            picking.write({"location_id": van.id})
            for move in picking.move_ids:
                move.write({"location_id": van.id})
                move.quantity = move.product_uom_qty  # oversold accepted: van quant may go negative
            picking.with_context(skip_backorder=True, skip_sms=True, skip_immediate=True).button_validate()

    def _register_cash(self, invoice, amount, company):
        Journal = self.env["account.journal"].sudo()
        journal = (Journal.search([("type", "=", "cash"), ("company_id", "=", company.id)], limit=1)
                   or Journal.search([("type", "=", "bank"), ("company_id", "=", company.id)], limit=1))
        if not journal:
            raise Problem(503, "not_configured", detail="no cash/bank journal for driver payments")
        wizard = self.env["account.payment.register"].sudo().with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "amount": amount, "journal_id": journal.id, "payment_difference_handling": "open"})
        wizard.action_create_payments()

    def sale_data(self, order):
        """Contract money: lines priced before the document discount (unit × qty, VAT half-up per line);
        discount_total = gross − Odoo total, so total = subtotal + tax_total − discount_total holds."""
        cur = order.currency_id
        scale = 10 ** cur.decimal_places
        mm = lambda minor: {"amount_minor": int(minor), "currency": cur.name}  # noqa: E731
        lines, subtotal, tax_total = [], 0, 0
        disc = order.env["product.product"].sudo().with_context(active_test=False).search(
            [("default_code", "=", "SWEAKA-DISCOUNT")], limit=1)  # read path: never create (no user on GET)
        for line in order.order_line.filtered(lambda l: not l.display_type and l.product_id != disc):
            taxes = line.tax_ids if "tax_ids" in line._fields else line.tax_id  # 19+: tax_ids; ≤18: tax_id
            tax = taxes.filtered(lambda t: t.amount_type == "percent")[:1]
            rate = tax.amount if tax else 0
            total = int(round(line.price_unit * scale)) * int(line.product_uom_qty)
            line_tax = int((2 * total * rate + 100) // 200) if rate else 0  # half-up of total × rate / 100
            subtotal, tax_total = subtotal + total, tax_total + line_tax
            lines.append({"product_id": str(line.product_id.id), "product_name": line.product_id.display_name,
                          "qty": int(line.product_uom_qty), "unit_price": mm(int(round(line.price_unit * scale))),
                          "tax_rate": _dec(rate), "total": mm(total), "tax": mm(line_tax)})
        total_minor = int(round(order.amount_total * scale))
        discount_minor = max(0, subtotal + tax_total - total_minor)
        m = lambda v: {"amount_minor": int(round((v or 0) * scale)), "currency": cur.name}  # noqa: E731
        to_utc = lambda dt: iso(dt.replace(tzinfo=timezone.utc)) if dt else None  # noqa: E731
        loc = ({"lat": order.sweaka_latitude, "lon": order.sweaka_longitude}
               if order.sweaka_latitude or order.sweaka_longitude else None)
        cancelled = order.state == "cancel"
        return {
            "id": str(order.id), "client_id": order.sweaka_client_id or None, "number": order.name,
            "customer_id": str(order.partner_id.id), "status": "cancelled" if cancelled else "completed",
            "lines": lines, "subtotal": mm(subtotal), "tax_total": mm(tax_total),
            "discount_total": mm(discount_minor), "total": mm(subtotal + tax_total - discount_minor),
            "paid": m(order.sweaka_paid),
            "client_total": m(order.sweaka_client_total) if order.sweaka_client_total else None,
            "location": loc, "location_captured_at": None, "note": order.sweaka_note or None,
            "sold_at": to_utc(order.sweaka_sold_at or order.date_order), "cancel_reason": None,
            "cancelled_at": to_utc(order.write_date) if cancelled else None,
            "created_at": to_utc(order.create_date), "updated_at": to_utc(order.write_date),
        }


def _dec(value):
    text = f"{float(value or 0):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def discount_product(env):
    """Untaxed service product carrying the driver's document discount (one negative line per order)."""
    Product = env["product.product"].sudo().with_context(active_test=False)
    product = Product.search([("default_code", "=", "SWEAKA-DISCOUNT")], limit=1)
    if not product:
        product = Product.create({"name": "Driver discount", "default_code": "SWEAKA-DISCOUNT", "type": "service",
                                  "sale_ok": False, "purchase_ok": False, "list_price": 0, "taxes_id": [(5, 0, 0)]})
    return product


def apply_discount(order, discount, company):
    """Document discount (contract): ≤ max_discount_percent of subtotal + tax (server-enforced); booked as one
    untaxed negative line so Odoo's total = gross − discount exactly. Replaces any previous discount line."""
    product = discount_product(order.env)
    order.order_line.filtered(lambda l: l.product_id == product).unlink()
    if not discount:
        return
    gross = order.amount_total
    if discount > gross * (company.sweaka_max_discount_percent or 0) / 100 + 1e-9:
        raise Problem.field("discount", "discount_too_high")
    tax_field = "tax_ids" if "tax_ids" in order.order_line._fields else "tax_id"
    order.write({"order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": 1, "price_unit": -discount,
                                        tax_field: [(5, 0, 0)], "name": "Driver discount"})]})
