import secrets

from odoo import fields, models


def _new_code():
    return f"{secrets.randbelow(100000):05d}"


class ResUsers(models.Model):
    _inherit = "res.users"

    sweaka_driver = fields.Boolean("Sweaka driver", help="May log in to the Sweaka Driver app.")
    sweaka_van_location_id = fields.Many2one(
        "stock.location", "Van stock location", domain=[("usage", "=", "internal")],
        help="Driver's van: van stock and sales are taken from this location.")
    sweaka_handover_code = fields.Char(
        "Handover code", copy=False, default=lambda self: _new_code(),
        help="Given face to face at the loading dock / cash desk: the driver types it to validate a van load or close "
             "the day. Rotates after each successful use.")
    sweaka_code_wrong = fields.Integer(copy=False, default=0)
    sweaka_code_window = fields.Datetime(copy=False)
