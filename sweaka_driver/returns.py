"""Contract return → Odoo: goods back into the van (stock move customers → van) + posted credit note
(out_refund) = customer credit (T9.6.7)."""
from datetime import timezone

from odoo import fields

from .lib.sweaka_connect_core import Problem
from .lib.sweaka_connect_core.feed import iso
from .sales import SaleService, _dec, _when

REASONS = ("damaged", "expired", "unsold", "wrong_item", "other")


class ReturnService:
    def __init__(self, wms):
        self.wms, self.env = wms, wms.env

    def _sale(self, body, partner):
        Sale = self.env["sale.order"]
        if body.get("sale_client_id") is not None:
            if body.get("sale_id") is not None:
                raise Problem.field("sale_client_id")
            sale = Sale.search([("sweaka_client_id", "=", str(body["sale_client_id"]))], limit=1)
            if not sale:
                raise Problem.field("sale_client_id")
        elif body.get("sale_id") is not None:
            sid = str(body["sale_id"])
            sale = Sale.browse(int(sid)).exists() if sid.isdigit() else Sale
            if not sale:
                raise Problem.field("sale_id")
        else:
            return Sale
        if sale.partner_id != partner:
            raise Problem.field("sale_id")
        return sale

    def _returned(self, sale, product):
        refunds = self.env["account.move"].search([("sweaka_sale_id", "=", sale.id), ("state", "=", "posted")])
        return sum(refunds.invoice_line_ids.filtered(lambda l: l.product_id == product).mapped("quantity"))

    def create(self, driver, body):
        user = self.wms._user(driver)
        self.env = self.env(user=user.id, su=True)
        company, cur = user.company_id, user.company_id.currency_id
        van = user.sweaka_van_location_id
        if not van:
            raise Problem(403, "forbidden", detail="driver has no van location")
        sales = SaleService(self.wms)
        sales.env = self.env
        partner = sales.customer(body, company)
        if body.get("reason") not in REASONS:
            raise Problem.field("reason")
        sale = self._sale(body, partner)
        lines = body.get("lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= 500:
            raise Problem.field("lines")
        Product, invoice_lines, seen, moves = self.env["product.product"], [], set(), []
        for i, line in enumerate(lines):
            pid = str(line.get("product_id")) if isinstance(line, dict) else ""
            product = Product.browse(int(pid)).exists() if pid.isdigit() else Product
            qty = line.get("qty") if isinstance(line, dict) else None
            if not product or product.id in seen:
                raise Problem.field(f"lines[{i}].product_id")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                raise Problem.field(f"lines[{i}].qty")
            if sale:
                sold = sale.order_line.filtered(lambda l, p=product: l.product_id == p)
                if not sold:
                    raise Problem.field(f"lines[{i}].product_id")
                if qty > sum(sold.mapped("product_uom_qty")) - self._returned(sale, product):
                    raise Problem.field(f"lines[{i}].qty", "exceeds_sold")
                price = sold[0].price_unit
            else:
                tier = sales.best_tier(product, partner, company, qty)
                if tier is None:
                    raise Problem.field(f"lines[{i}].product_id", "no_price")
                price = tier["unit_price"]["amount_minor"] / 10 ** cur.decimal_places
            seen.add(product.id)
            invoice_lines.append((0, 0, {"product_id": product.id, "quantity": qty, "price_unit": price}))
            moves.append((product, qty))
        returned_at = _when(body.get("returned_at"), "returned_at")
        refund = self.env["account.move"].with_company(company).create({
            "move_type": "out_refund", "partner_id": partner.id, "invoice_line_ids": invoice_lines,
            "sweaka_client_id": body["client_id"], "sweaka_driver_id": user.id, "sweaka_sale_id": sale.id or False,
            "sweaka_reason": body["reason"], "sweaka_returned_at": returned_at, "sweaka_note": body.get("note")})
        refund.action_post()
        customers = self.env.ref("stock.stock_location_customers")
        for product, qty in moves:  # goods physically back in the van, whatever the reason
            Move = self.env["stock.move"]
            extra = {"name": product.display_name} if "name" in Move._fields else {}  # ≤18 requires name
            uom_field = "product_uom" if "product_uom" in Move._fields else "uom_id"  # 20 renamed it
            move = Move.create({**extra, "product_id": product.id, "product_uom_qty": qty,
                                                  uom_field: product.uom_id.id, "location_id": customers.id,
                                                  "location_dest_id": van.id, "company_id": company.id,
                                                  "origin": refund.name})
            move._action_confirm()
            move.quantity = qty
            move.picked = True
            move._action_done()
        partner.write({"sweaka_touched_at": fields.Datetime.now()})
        return self.return_data(refund)

    def return_data(self, refund):
        cur = refund.currency_id
        m = lambda v: {"amount_minor": int(round((v or 0) * 10 ** cur.decimal_places)), "currency": cur.name}  # noqa: E731
        to_utc = lambda dt: iso(dt.replace(tzinfo=timezone.utc)) if dt else None  # noqa: E731
        lines = []
        for line in refund.invoice_line_ids.filtered(lambda l: l.display_type == "product"):
            tax = line.tax_ids.filtered(lambda t: t.amount_type == "percent")[:1]
            lines.append({"product_id": str(line.product_id.id), "product_name": line.product_id.display_name,
                          "qty": int(line.quantity), "unit_price": m(line.price_unit),
                          "tax_rate": _dec(tax.amount if tax else 0), "total": m(line.price_subtotal),
                          "tax": m(line.price_total - line.price_subtotal)})
        cancelled = refund.state == "cancel"
        return {"id": str(refund.id), "client_id": refund.sweaka_client_id or None, "number": refund.name,
                "customer_id": str(refund.partner_id.id), "sale_id": str(refund.sweaka_sale_id.id) if refund.sweaka_sale_id else None,
                "status": "cancelled" if cancelled else "completed", "reason": refund.sweaka_reason or "other",
                "lines": lines, "subtotal": m(refund.amount_untaxed), "tax_total": m(refund.amount_tax),
                "total": m(refund.amount_total), "location": None, "note": refund.sweaka_note or None,
                "returned_at": to_utc(refund.sweaka_returned_at), "cancel_reason": None,
                "cancelled_at": to_utc(refund.write_date) if cancelled else None,
                "created_at": to_utc(refund.create_date), "updated_at": to_utc(refund.write_date)}
