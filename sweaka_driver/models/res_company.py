import base64
import io

from odoo import api, fields, models

from ..lib.sweaka_connect_core.server_link import server_link


class ResCompany(models.Model):
    _inherit = "res.company"

    sweaka_debt_allowed = fields.Boolean("Drivers may sell on credit", default=True)
    sweaka_max_discount_percent = fields.Integer("Max driver discount (%)", default=0,
                                                 help="0 = drivers cannot give discounts.")
    sweaka_gps_required = fields.Boolean("Driver must record GPS", default=False)
    sweaka_drivers_can_sign_out = fields.Boolean("Drivers may sign out", default=True)
    sweaka_proof_required = fields.Selection([("none", "None"), ("photo", "Photo"), ("signature", "Signature"),
                                              ("both", "Photo + signature")], "Delivery proof", default="none")
    sweaka_upload_retention_days = fields.Integer("Keep delivery proofs (days)", default=365)
    # T9.7.1.11 per-driver licence (Sweaka licensing portal → company code + plugin secret)
    sweaka_licence_url = fields.Char("Sweaka licensing URL", default="https://api.sweaka.com", groups="base.group_system")
    sweaka_licence_tenant_key = fields.Char("Sweaka company code", groups="base.group_system")
    sweaka_licence_secret = fields.Char("Sweaka plugin secret", groups="base.group_system", copy=False)
    sweaka_licence_last_sync = fields.Datetime("Last driver sync", readonly=True, copy=False)
    sweaka_licence_last_error = fields.Char("Last sync error", readonly=True, copy=False)
    sweaka_licence_seats = fields.Integer("Billed drivers", readonly=True, copy=False)

    # T9.1.2d.1 driver app server QR (contract ServerLink)
    sweaka_server_link = fields.Char("Driver app server code", compute="_compute_sweaka_server_link")
    sweaka_server_link_error = fields.Char("Server code problem", compute="_compute_sweaka_server_link")
    sweaka_server_qr = fields.Binary("Driver app QR code", compute="_compute_sweaka_server_link")

    @api.depends("name")
    def _compute_sweaka_server_link(self):
        icp = self.env["ir.config_parameter"].sudo()
        base = (icp.get_str if hasattr(icp, "get_str") else icp.get_param)("web.base.url") or ""  # Odoo 20 renamed
        for company in self:
            company.sweaka_server_qr = False
            try:
                link = server_link(f"{base.rstrip('/')}/sweaka/driver", company.id, company.name)
            except ValueError as exc:
                company.sweaka_server_link, company.sweaka_server_link_error = False, str(exc)
                continue
            company.sweaka_server_link, company.sweaka_server_link_error = link, False
            try:
                import qrcode  # Odoo dependency (account QR codes)

                buf = io.BytesIO()
                qrcode.make(link, border=2).save(buf, format="PNG")
                company.sweaka_server_qr = base64.b64encode(buf.getvalue()).decode()  # str: Odoo 17–20
            except ImportError:
                company.sweaka_server_link_error = "python qrcode missing: give drivers the code text instead"

    def action_sweaka_licence_push(self):
        self.env["sweaka.driver.licence"].push(self)
        return True


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    sweaka_debt_allowed = fields.Boolean(related="company_id.sweaka_debt_allowed", readonly=False)
    sweaka_max_discount_percent = fields.Integer(related="company_id.sweaka_max_discount_percent", readonly=False)
    sweaka_gps_required = fields.Boolean(related="company_id.sweaka_gps_required", readonly=False)
    sweaka_drivers_can_sign_out = fields.Boolean(related="company_id.sweaka_drivers_can_sign_out", readonly=False)
    sweaka_proof_required = fields.Selection(related="company_id.sweaka_proof_required", readonly=False)
    sweaka_upload_retention_days = fields.Integer(related="company_id.sweaka_upload_retention_days", readonly=False)
