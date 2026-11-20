from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    sweaka_client_id = fields.Char("Sweaka app id", index=True, copy=False, readonly=True)
    sweaka_driver_id = fields.Many2one("res.users", "Sweaka driver", index=True, copy=False, readonly=True)
    sweaka_sale_id = fields.Many2one("sale.order", "Returned from sale", copy=False, readonly=True)
    sweaka_reason = fields.Selection([("damaged", "Damaged"), ("expired", "Expired"), ("unsold", "Unsold"),
                                      ("wrong_item", "Wrong item"), ("other", "Other")], copy=False, readonly=True)
    sweaka_returned_at = fields.Datetime("Returned at (device time)", copy=False, readonly=True)
    sweaka_note = fields.Text("Driver note", copy=False, readonly=True)
