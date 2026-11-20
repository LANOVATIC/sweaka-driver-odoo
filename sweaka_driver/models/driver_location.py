"""Driver GPS tracks (contract POST /gps/batches, T9.6.7): manager-only, purged after retention days."""
from datetime import timedelta

from odoo import api, fields, models


class SweakaGpsBatch(models.Model):
    _name = "sweaka.driver.gps.batch"
    _description = "Sweaka driver GPS batch"

    client_id = fields.Char(required=True, index=True)
    driver_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    points_count = fields.Integer()
    first_captured_at = fields.Datetime()
    last_captured_at = fields.Datetime()

    if hasattr(models, "Constraint"):
        _client_uniq = models.Constraint("unique(client_id)", "One batch per client_id.")
    else:
        _sql_constraints = [("client_uniq", "unique(client_id)", "One batch per client_id.")]


class SweakaDriverLocation(models.Model):
    _name = "sweaka.driver.location"
    _description = "Sweaka driver GPS point"
    _order = "captured_at"

    driver_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    batch_id = fields.Many2one("sweaka.driver.gps.batch", ondelete="cascade", index=True)
    latitude = fields.Float(digits=(10, 7), required=True)
    longitude = fields.Float(digits=(10, 7), required=True)
    accuracy_m = fields.Integer()
    speed_kmh = fields.Integer()
    captured_at = fields.Datetime(required=True, index=True)

    @api.model
    def _cron_purge(self):
        icp = self.env["ir.config_parameter"].sudo()
        days = int((icp.get_str if hasattr(icp, "get_str") else icp.get_param)("sweaka_driver.gps_retention_days") or 60)  # Odoo 20 renamed
        cutoff = fields.Datetime.now() - timedelta(days=days)
        self.sudo().search([("captured_at", "<", cutoff)]).unlink()
        self.env["sweaka.driver.gps.batch"].sudo().search([("last_captured_at", "<", cutoff)]).unlink()
