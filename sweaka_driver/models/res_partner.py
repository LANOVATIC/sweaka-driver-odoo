from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    sweaka_client_id = fields.Char("Sweaka app id", index=True, copy=False, readonly=True,
                                   help="client_id (UUID) of a customer created in the Sweaka Driver app.")
    sweaka_touched_at = fields.Datetime(copy=False, readonly=True,
                                        help="Bumped when the balance moves (sale/payment) so the driver feed re-sends it.")
