"""T9.7.1.11: push the company's full driver list to Sweaka licensing (billing per active driver)."""
import logging

from odoo import api, fields, models

from ..lib.sweaka_connect_core.licensing import LicensingError, push_drivers, request_sync_push

DRIVER_FIELDS = ("sweaka_driver", "active", "name")
_logger = logging.getLogger(__name__)


class SweakaDriverLicence(models.AbstractModel):
    _name = "sweaka.driver.licence"
    _description = "Sweaka driver licence sync"

    def _drivers(self, company):
        users = self.env["res.users"].sudo().with_context(active_test=False).search(
            [("sweaka_driver", "=", True), ("company_ids", "in", company.id)])
        return [(u.id, u.name, u.active) for u in users]

    def push(self, companies=None):
        """Errors are stored on the company (shown in settings), never raised: a cron must not fail the module."""
        companies = (companies or self.env["res.company"].search([])).sudo()
        for company in companies.filtered(lambda c: c.sweaka_licence_tenant_key and c.sweaka_licence_secret):
            try:
                out = push_drivers(company.sweaka_licence_tenant_key, company.sweaka_licence_secret,
                                   self._drivers(company),
                                   base_url=company.sweaka_licence_url or "https://api.sweaka.com")
                company.write({"sweaka_licence_last_sync": fields.Datetime.now(), "sweaka_licence_last_error": False,
                               "sweaka_licence_seats": int(out.get("seats") or 0)})
            except (LicensingError, ValueError) as exc:
                company.write({"sweaka_licence_last_error": str(exc)[:255]})

    @api.model
    def _cron_push(self):
        self.push()

    def wake_drivers(self, users):
        """T9.7.2: content-free "sync now" to these driver users' phones (after commit; failures only logged)."""
        users = users.filtered("sweaka_driver")
        for company in users.mapped("company_id").sudo():
            ids = users.filtered(lambda u, c=company: u.company_id == c).ids
            if not (ids and company.sweaka_licence_tenant_key and company.sweaka_licence_secret):
                continue
            args = (company.sweaka_licence_tenant_key, company.sweaka_licence_secret, ids,
                    company.sweaka_licence_url or "https://api.sweaka.com")

            def send(args=args):
                try:
                    request_sync_push(*args[:3], base_url=args[3])
                except (LicensingError, ValueError) as exc:
                    _logger.info("sweaka sync push skipped: %s", exc)

            self.env.cr.postcommit.add(send)


class ResUsersLicence(models.Model):
    _inherit = "res.users"

    def _sweaka_licence_soon(self):
        cron = self.env.ref("sweaka_driver.cron_licence_push", raise_if_not_found=False)
        if cron and hasattr(cron, "_trigger"):
            cron.sudo()._trigger()  # run shortly, outside this transaction (billing follows driver changes)

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        if any(v.get("sweaka_driver") for v in vals_list):
            users._sweaka_licence_soon()
        return users

    def write(self, vals):
        res = super().write(vals)
        if any(f in vals for f in DRIVER_FIELDS) and (vals.get("sweaka_driver") or any(self.mapped("sweaka_driver"))
                                                      or "sweaka_driver" in vals):
            self._sweaka_licence_soon()
        return res


class StockPickingWake(models.Model):
    _inherit = "stock.picking"

    def action_confirm(self):
        res = super().action_confirm()
        vans = self.filtered(lambda p: p.picking_type_code == "internal").mapped("location_dest_id")
        if vans:
            drivers = self.env["res.users"].sudo().search([("sweaka_driver", "=", True),
                                                          ("sweaka_van_location_id", "in", vans.ids)])
            self.env["sweaka.driver.licence"].wake_drivers(drivers)  # van load ready to validate
        return res


class SaleOrderWake(models.Model):
    _inherit = "sale.order"

    def _sweaka_wake(self):
        orders = self.filtered(lambda o: o.sweaka_kind == "order" and o.sweaka_driver_id)
        if orders:
            self.env["sweaka.driver.licence"].wake_drivers(orders.mapped("sweaka_driver_id"))

    def action_confirm(self):
        res = super().action_confirm()
        self._sweaka_wake()  # driver's order approved by the back office
        return res

    def _action_cancel(self):
        res = super()._action_cancel()
        self._sweaka_wake()
        return res
