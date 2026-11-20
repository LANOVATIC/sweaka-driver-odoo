"""Deliveries (T9.1.4h.8, owner-approved 2026-11-20): driver orders' outgoing pickings, events, proof uploads."""
from datetime import timedelta

from odoo import api, fields, models

STATUSES = [("assigned", "Assigned"), ("out_for_delivery", "Out for delivery"), ("delivered", "Delivered"),
            ("partially_delivered", "Partially delivered"), ("failed", "Failed"), ("returned", "Returned to depot"),
            ("cancelled", "Cancelled")]
REASONS = [("absent", "Absent"), ("refused", "Refused"), ("address_wrong", "Wrong address"), ("damaged", "Damaged"),
           ("other", "Other")]


class StockPicking(models.Model):
    _inherit = "stock.picking"

    sweaka_driver_id = fields.Many2one("res.users", "Sweaka driver", index=True, copy=False,
                                       compute="_compute_sweaka_driver", store=True, readonly=False,
                                       help="Driver delivering this picking (default: the driver of the app order).")
    sweaka_delivery_status = fields.Selection(STATUSES, "Delivery status", default="assigned", copy=False)
    sweaka_attempts = fields.Integer("Delivery attempts", copy=False)
    sweaka_failure_reason = fields.Selection(REASONS, copy=False)
    sweaka_delivered_at = fields.Datetime("Delivered at (device time)", copy=False)
    sweaka_last_event_at = fields.Datetime(copy=False)
    sweaka_sequence = fields.Integer("Route order", copy=False)
    sweaka_recipient_name = fields.Char("Received by", copy=False)
    sweaka_photo_ids = fields.Many2many("sweaka.driver.upload", string="Proof photos", copy=False)
    sweaka_signature_id = fields.Many2one("sweaka.driver.upload", "Proof signature", copy=False)
    sweaka_delivered_json = fields.Json(copy=False)  # {move_id: [qty, delivered_qty]} frozen at validation

    @api.depends("sale_id.sweaka_driver_id", "sale_id.sweaka_kind", "picking_type_code")
    def _compute_sweaka_driver(self):
        for pick in self:
            order = pick.sale_id
            if not pick.sweaka_driver_id and pick.picking_type_code == "outgoing" and order.sweaka_kind == "order":
                pick.sweaka_driver_id = order.sweaka_driver_id
            else:
                pick.sweaka_driver_id = pick.sweaka_driver_id


class SweakaDeliveryEvent(models.Model):
    _name = "sweaka.delivery.event"
    _description = "Sweaka driver delivery event"
    _order = "id desc"

    client_id = fields.Char(index=True, readonly=True, required=True)
    picking_id = fields.Many2one("stock.picking", required=True, index=True, readonly=True, ondelete="cascade")
    driver_id = fields.Many2one("res.users", required=True, index=True, readonly=True)
    type = fields.Selection([("start", "Start"), ("deliver", "Deliver"), ("partial", "Partial"), ("fail", "Fail"),
                             ("return", "Return")], required=True, readonly=True)
    at = fields.Datetime(required=True, readonly=True)
    applied = fields.Boolean(readonly=True)
    reason = fields.Selection(REASONS, readonly=True)
    note = fields.Text(readonly=True)
    latitude = fields.Float(digits=(10, 7), readonly=True)
    longitude = fields.Float(digits=(10, 7), readonly=True)


class SweakaDriverUpload(models.Model):
    _name = "sweaka.driver.upload"
    _description = "Sweaka driver proof upload (photo / signature)"
    _order = "id desc"

    client_id = fields.Char(index=True, readonly=True, required=True)
    driver_id = fields.Many2one("res.users", required=True, index=True, readonly=True)
    sha256 = fields.Char(readonly=True, required=True)
    size = fields.Integer(readonly=True)
    content_type = fields.Char(readonly=True)
    attachment_id = fields.Many2one("ir.attachment", readonly=True, ondelete="set null")

    @api.model
    def _cron_purge(self):
        """Retention per company (default 365 days, owner 2026-11-20): old proofs and their files go."""
        for company in self.env["res.company"].search([]):
            days = company.sweaka_upload_retention_days or 365
            old = self.search([("driver_id.company_id", "=", company.id),
                               ("create_date", "<", fields.Datetime.now() - timedelta(days=days))])
            old.attachment_id.unlink()
            old.unlink()
