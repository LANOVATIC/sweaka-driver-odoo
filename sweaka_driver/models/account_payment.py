from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = "account.payment"

    sweaka_client_id = fields.Char("Sweaka app id", index=True, copy=False, readonly=True)
    sweaka_driver_id = fields.Many2one("res.users", "Sweaka driver", index=True, copy=False, readonly=True)
    sweaka_method = fields.Selection([("cash", "Cash"), ("card", "Card"), ("bank", "Bank"), ("other", "Other")],
                                     copy=False, readonly=True)
    sweaka_paid_at = fields.Datetime("Collected at (device time)", copy=False, readonly=True)
    sweaka_note = fields.Text("Driver note", copy=False, readonly=True)
    sweaka_delivery_id = fields.Many2one("stock.picking", "Cash on delivery for", copy=False, readonly=True)
