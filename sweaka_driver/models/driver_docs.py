"""Driver-declared documents waiting for the back office: stock losses, cash handovers; day closes (T9.6.12/13)."""
from odoo import api, fields, models
from odoo.exceptions import UserError

STATES = [("pending", "Pending"), ("validated", "Validated"), ("rejected", "Rejected")]


class SweakaStockLoss(models.Model):
    _name = "sweaka.stock.loss"
    _description = "Sweaka driver stock loss"
    _order = "id desc"

    name = fields.Char(default=lambda self: self.env["ir.sequence"].next_by_code("sweaka.stock.loss") or "/",
                       readonly=True)
    client_id = fields.Char(index=True, readonly=True)
    driver_id = fields.Many2one("res.users", required=True, index=True, readonly=True)
    state = fields.Selection(STATES, default="pending", required=True)
    reason = fields.Selection([("damaged", "Damaged"), ("lost", "Lost"), ("expired", "Expired"), ("other", "Other")],
                              required=True)
    note = fields.Text()
    declared_at = fields.Datetime()
    validated_at = fields.Datetime(readonly=True)
    rejected_at = fields.Datetime(readonly=True)
    reject_reason = fields.Char()
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    line_ids = fields.One2many("sweaka.stock.loss.line", "loss_id")

    def action_validate(self):
        """Back office: move each line out of the driver's van (stock.scrap on 17–19; Odoo 20 dropped it → done
        stock.move to the company's inventory-loss location, same as the Bridge, T9.6.12a)."""
        for loss in self.filtered(lambda r: r.state == "pending"):
            van = loss.driver_id.sweaka_van_location_id
            company = loss.driver_id.company_id
            for line in loss.line_ids:
                if "stock.scrap" in self.env:
                    scrap = self.env["stock.scrap"].with_company(company).create(
                        {"product_id": line.product_id.id, "scrap_qty": line.qty, "location_id": van.id,
                         "origin": loss.name})
                    scrap.action_validate()
                else:
                    loss._move_to_inventory_loss(line, van, company)
            loss.write({"state": "validated", "validated_at": fields.Datetime.now()})

    def _move_to_inventory_loss(self, line, van, company):
        dest = self.env["stock.location"].search([("usage", "=", "inventory"),
                                                  ("company_id", "in", [company.id, False])], limit=1)
        if not dest:
            raise UserError("No inventory-loss location for this company.")
        Move = self.env["stock.move"]
        extra = {"name": line.product_id.display_name} if "name" in Move._fields else {}
        uom_field = "product_uom" if "product_uom" in Move._fields else "uom_id"
        move = Move.create({**extra, "product_id": line.product_id.id, "product_uom_qty": line.qty,
                            uom_field: line.product_id.uom_id.id, "location_id": van.id, "location_dest_id": dest.id,
                            "company_id": company.id, "origin": self.name})
        move._action_confirm()
        move.quantity = line.qty
        move.picked = True
        move._action_done()

    def action_reject(self):
        self.filtered(lambda r: r.state == "pending").write({"state": "rejected", "rejected_at": fields.Datetime.now()})


class SweakaStockLossLine(models.Model):
    _name = "sweaka.stock.loss.line"
    _description = "Sweaka driver stock loss line"

    loss_id = fields.Many2one("sweaka.stock.loss", required=True, ondelete="cascade")
    product_id = fields.Many2one("product.product", required=True)
    qty = fields.Integer(required=True)


class SweakaCashHandover(models.Model):
    _name = "sweaka.cash.handover"
    _description = "Sweaka driver cash handover"
    _order = "id desc"

    name = fields.Char(default=lambda self: self.env["ir.sequence"].next_by_code("sweaka.cash.handover") or "/",
                       readonly=True)
    client_id = fields.Char(index=True, readonly=True)
    driver_id = fields.Many2one("res.users", required=True, index=True, readonly=True)
    state = fields.Selection(STATES, default="pending", required=True)
    amount = fields.Float(required=True)
    currency_id = fields.Many2one("res.currency", required=True)
    note = fields.Text()
    declared_at = fields.Datetime()
    validated_at = fields.Datetime(readonly=True)
    rejected_at = fields.Datetime(readonly=True)
    reject_reason = fields.Char()
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))

    def action_validate(self):
        self.filtered(lambda r: r.state == "pending").write({"state": "validated", "validated_at": fields.Datetime.now()})

    def action_reject(self):
        self.filtered(lambda r: r.state == "pending").write({"state": "rejected", "rejected_at": fields.Datetime.now()})


class SweakaDayClose(models.Model):
    _name = "sweaka.day.close"
    _description = "Sweaka driver day close (remaining van stock snapshot)"
    _order = "id desc"

    name = fields.Char(readonly=True)
    driver_id = fields.Many2one("res.users", required=True, index=True, readonly=True)
    note = fields.Text(readonly=True)
    lines_json = fields.Json(readonly=True)
    lines_text = fields.Text("Van stock at close", compute="_compute_lines_text")  # Json has no form widget (17-20)

    @api.depends("lines_json")
    def _compute_lines_text(self):
        for rec in self:
            rec.lines_text = "\n".join(f"{line.get('qty', 0):>6}  {line.get('product_name', '')}"
                                        for line in rec.lines_json or []) or False
