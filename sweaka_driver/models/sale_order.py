from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    sweaka_client_id = fields.Char("Sweaka app id", index=True, copy=False, readonly=True)
    sweaka_driver_id = fields.Many2one("res.users", "Sweaka driver", index=True, copy=False, readonly=True)
    sweaka_paid = fields.Monetary("Cash collected at sale", copy=False, readonly=True)
    sweaka_client_total = fields.Monetary("Total shown on the phone", copy=False, readonly=True,
                                          help="Price-mismatch review: differs from the server total → check prices.")
    sweaka_sold_at = fields.Datetime("Sold at (device time)", copy=False, readonly=True)
    sweaka_note = fields.Text("Driver note", copy=False, readonly=True)
    sweaka_kind = fields.Selection([("sale", "Van sale"), ("order", "Order for later delivery")], copy=False,
                                   readonly=True, index=True)
    sweaka_cancel_reason = fields.Char(copy=False, readonly=True)
    sweaka_latitude = fields.Float("Sale latitude", digits=(10, 7), copy=False, readonly=True)
    sweaka_longitude = fields.Float("Sale longitude", digits=(10, 7), copy=False, readonly=True)
