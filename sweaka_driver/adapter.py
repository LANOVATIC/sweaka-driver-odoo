"""Odoo adapter: WmsPort + StatePort over the Odoo ORM (T9.6). Records are mapped to contract JSON here."""
import logging
from datetime import datetime, timedelta, timezone

from odoo.exceptions import AccessDenied

from .lib.sweaka_connect_core import Problem
from .lib.sweaka_connect_core.feed import iso
from .lib.sweaka_connect_core.ports import Change, Driver
from .deliveries import DeliveryService
from .docs import DocService
from .gps import GpsService
from .orders import OrderService
from .payments import PaymentService
from .returns import ReturnService
from .sales import SaleService

_logger = logging.getLogger(__name__)
CAPS = ("customers_create", "customers_update_location", "orders", "deliveries", "van_sales", "payments",
        "returns", "stock_loss", "van_stock", "stock_validation", "day_close", "gps_tracking")
IMPLEMENTED = {"van_stock", "customers_create", "van_sales", "payments", "returns", "gps_tracking", "orders",
               "stock_loss", "stock_validation", "day_close", "customers_update_location", "deliveries"}  # grows with T9.6.5+ (sales, payments, ...)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utc(dt):
    """Odoo stores naive UTC datetimes."""
    return dt.replace(tzinfo=timezone.utc) if dt else EPOCH


def _minor(amount, currency):
    return {"amount_minor": int(round((amount or 0) * 10 ** currency.decimal_places)), "currency": currency.name}


def _wc(records, company):
    """with_company that also works without a session user (Odoo 20 needs allowed_company_ids in context)."""
    return records.with_context(allowed_company_ids=company.ids).with_company(company)


def _named(record):
    return {"id": str(record.id), "name": record.display_name or record.name or ""} if record else None


def _iso_currency(company):
    return (company.currency_id.name or "EUR")[:3]


class OdooWms:
    def __init__(self, env):
        self.env = env

    # ---- identity ----------------------------------------------------------------------------------------------
    def _driver(self, user):
        return Driver(id=str(user.id), tenant_id=str(user.company_id.id), name=user.name, extra={"uid": user.id})

    def capabilities(self):
        return {c: c in IMPLEMENTED for c in CAPS}

    def authenticate(self, username, password):
        users = self.env["res.users"].sudo()
        credential = {"type": "password", "login": username, "password": password}
        try:
            uid = _authenticate(users, self.env.cr.dbname, credential)
        except AccessDenied:
            return None
        user = users.browse(uid)
        if not user.sweaka_driver:
            raise Problem(403, "forbidden", detail="user is not a Sweaka driver")
        return self._driver(user)

    def driver(self, driver_id):
        user = self.env["res.users"].sudo().browse(int(driver_id)).exists()
        return self._driver(user) if user and user.active and user.sweaka_driver else None

    def profile(self, driver):
        user = self.env["res.users"].sudo().browse(int(driver.id))
        company = user.company_id
        return {
            "driver": {"id": driver.id, "name": user.name, "phone": user.partner_id.phone or None, "avatar_url": None},
            "tenant": {"id": str(company.id), "name": company.name, "address": " ".join((company.partner_id.contact_address or "").split()) or None,
                       "phone": company.phone or None, "email": company.email or None, "logo_url": None},
            "settings": {"currency": _iso_currency(company), "default_delivery_days": 2,
                         "can_sign_out": bool(company.sweaka_drivers_can_sign_out), "hide_today_money": False,
                         "gps_required": bool(company.sweaka_gps_required),
                         "debt_allowed": bool(company.sweaka_debt_allowed),
                         "max_discount_percent": int(company.sweaka_max_discount_percent or 0),
                         "proof_required": company.sweaka_proof_required or "none"},
        }

    # ---- data: products / prices / van stock / customers / driver cash (T9.6.3, T9.6.4) ---------------------------
    def _user(self, driver):
        return self.env["res.users"].sudo().browse(int(driver.id))

    def _pricelists(self, company):
        """Driver pricelists = pricelists in use by the company's customers (+ company default)."""
        return self.env["product.pricelist"].sudo().search(["|", ("company_id", "=", False),
                                                             ("company_id", "=", company.id)])

    def _tiers(self, product, company):
        """Pricelist fixed-price items → contract tiers scoped to the pricelist (= Customer.category);
        list price → unscoped base tier."""
        cur = company.currency_id
        tiers = [{"id": f"lst-{product.id}", "min_qty": 0, "max_qty": None, "unit_price": _minor(product.lst_price, cur),
                  "customer_category_ids": [], "zone_ids": []}]
        items = self.env["product.pricelist.item"].sudo().search([
            ("pricelist_id", "in", self._pricelists(company).ids), ("compute_price", "=", "fixed"), "|", "|",
            ("product_id", "=", product.id), ("product_tmpl_id", "=", product.product_tmpl_id.id),
            ("applied_on", "=", "3_global")])
        for item in items:
            tiers.append({"id": f"item-{item.id}", "min_qty": int(item.min_quantity or 0), "max_qty": None,
                          "unit_price": _minor(item.fixed_price, cur),
                          "customer_category_ids": [str(item.pricelist_id.id)], "zone_ids": []})
        return tiers, max([_utc(product.write_date)] + [_utc(i.write_date) for i in items])

    def product_data(self, product, company):
        tiers, ts = self._tiers(product, company)
        tax = product.taxes_id.filtered(lambda t: t.amount_type == "percent")[:1]
        categ = product.categ_id
        return {"id": str(product.id), "name": product.display_name, "sku": product.default_code or None,
                "barcode": product.barcode or None, "image_url": None,
                "category": _named(categ.parent_id or categ), "subcategory": _named(categ) if categ.parent_id else None,
                "uom": product.uom_id.name, "pack_size": 1, "tax_rate": _dec(tax.amount if tax else 0),
                "active": bool(product.active and product.sale_ok), "prices": tiers, "updated_at": iso(ts)}, ts

    def partner_pricelist(self, partner, company):
        """Customer.category = its pricelist (19: specific_* holds the choice when the pricelist group is off)."""
        scoped = _wc(partner, company)
        specific = (scoped.specific_property_product_pricelist if "specific_property_product_pricelist" in scoped._fields
                    else self.env["product.pricelist"])
        return specific or scoped.property_product_pricelist

    def customer_data(self, partner, company):
        cur = company.currency_id
        pricelist = self.partner_pricelist(partner, company)
        loc = None
        if partner.partner_latitude or partner.partner_longitude:
            loc = {"lat": partner.partner_latitude, "lon": partner.partner_longitude}
        return {"id": str(partner.id), "client_id": partner.sweaka_client_id or None, "name": partner.name,
                "phone": partner.phone or None, "address": " ".join((partner.contact_address or "").split()) or None,
                "city": {"id": partner.city, "name": partner.city} if partner.city else None, "location": loc,
                "category": _named(pricelist), "zones": [_named(t) for t in partner.category_id],
                "active": bool(partner.active), "balance": _minor(_wc(partner, company).credit, cur),
                "credit_limit": None, "updated_at": iso(_utc(partner.write_date))}

    def changes(self, driver, after, limit, snapshot):
        user = self._user(driver)
        company = user.company_id
        since = after.changed_at.replace(tzinfo=None) if after else None
        rows = []

        def window(model, domain, active_field="active"):
            dom = list(domain) + ([("write_date", ">=", since)] if since else [])
            env = self.env[model].sudo().with_context(active_test=False)
            if snapshot and active_field:
                dom.append((active_field, "=", True))
            return env.search(dom, order="write_date asc, id asc", limit=limit + 1)

        for product in window("product.product", [("sale_ok", "=", True), "|", ("company_id", "=", False),
                                                  ("company_id", "=", company.id)]):
            data, ts = self.product_data(product, company)
            rows.append(Change("product", str(product.id), ts, data))
        for partner in window("res.partner", [("customer_rank", ">", 0), "|", ("company_id", "=", False),
                                              ("company_id", "=", company.id)]):
            rows.append(Change("customer", str(partner.id), max(_utc(partner.write_date), _utc(partner.sweaka_touched_at)),
                               self.customer_data(partner, company) if partner.active else None,
                               partner.sweaka_client_id or None))
        van = user.sweaka_van_location_id
        if van:
            for quant in window("stock.quant", [("location_id", "=", van.id)], active_field=None):
                rows.append(Change("van_stock", str(quant.id), _utc(quant.write_date),
                                   {"id": str(quant.id), "product_id": str(quant.product_id.id),
                                    "qty": int(quant.quantity), "updated_at": iso(_utc(quant.write_date))}))
        for order in window("sale.order", [("sweaka_driver_id", "=", user.id), ("sweaka_kind", "!=", "order"),
                                           ("state", "in", ["sale", "done", "cancel"])], active_field=None):
            rows.append(Change("sale", str(order.id), _utc(order.write_date), SaleService(self).sale_data(order),
                               order.sweaka_client_id or None))
        for order in window("sale.order", [("sweaka_driver_id", "=", user.id), ("sweaka_kind", "=", "order")],
                            active_field=None):
            rows.append(Change("order", str(order.id), _utc(order.write_date), OrderService(self).order_data(order),
                               order.sweaka_client_id or None))
        for pay in window("account.payment", [("sweaka_driver_id", "=", user.id)], active_field=None):
            rows.append(Change("payment", str(pay.id), _utc(pay.write_date), PaymentService(self).payment_data(pay),
                               pay.sweaka_client_id or None))
        for ref in window("account.move", [("sweaka_driver_id", "=", user.id), ("move_type", "=", "out_refund"),
                                           ("state", "in", ["posted", "cancel"])], active_field=None):
            rows.append(Change("return", str(ref.id), _utc(ref.write_date), ReturnService(self).return_data(ref),
                               ref.sweaka_client_id or None))
        for model, rtype, render in (("sweaka.stock.loss", "stock_loss", DocService.loss_data),
                                     ("sweaka.cash.handover", "cash_handover", DocService.handover_data)):
            for rec in window(model, [("driver_id", "=", user.id)], active_field=None):
                rows.append(Change(rtype, str(rec.id), _utc(rec.write_date), render(rec), rec.client_id or None))
        if van:
            for pick in window("stock.picking", [("location_dest_id", "=", van.id), ("picking_type_code", "=", "internal"),
                                                 ("state", "not in", ("draft", "cancel"))], active_field=None):
                rows.append(Change("van_load", str(pick.id), _utc(pick.write_date), DocService.van_load_data(pick)))
        for pick in window("stock.picking", DeliveryService.domain(user), active_field=None):
            rows.append(Change("delivery", str(pick.id), _utc(pick.write_date), DeliveryService.delivery_data(pick)))
        rows.append(self._driver_cash(driver, user, company))
        rows = [c for c in rows if after is None or c.key > after]
        return sorted(rows, key=lambda c: c.key)[: limit + 1]

    def _driver_cash(self, driver, user, company):
        """cash on hand = cash taken at sales + payments collected by the driver (handovers: T9.6.x day close);
        credit total = Σ customer balances. ts = last driver document / user change."""
        cur = company.currency_id
        sales = self.env["sale.order"].sudo().search([("sweaka_driver_id", "=", user.id), ("state", "in", ["sale", "done"])])
        pays = self.env["account.payment"].sudo().search([("sweaka_driver_id", "=", user.id), ("state", "!=", "canceled")])
        handed = self.env["sweaka.cash.handover"].sudo().search([("driver_id", "=", user.id), ("state", "=", "validated")])
        cash = sum(sales.mapped("sweaka_paid")) + sum(pays.mapped("amount")) - sum(handed.mapped("amount"))
        partners = self.env["res.partner"].sudo().search([("customer_rank", ">", 0), "|", ("company_id", "=", False),
                                                          ("company_id", "=", company.id)])
        credit = sum(_wc(partners, company).mapped("credit"))
        ts = max([_utc(user.write_date)] + [_utc(d) for d in sales.mapped("write_date") + pays.mapped("write_date")])
        return Change("driver_cash", driver.id, ts, {"id": driver.id, "cash_on_hand": _minor(cash, cur),
                                                     "customer_credit_total": _minor(credit, cur), "updated_at": iso(ts)})

    def _set_location(self, driver, customer_id, body):
        """Field-level stale rule (contract "Location"): only the position is compared (±1e-6°)."""
        company = self._user(driver).company_id
        Partner = self.env["res.partner"].sudo()
        partner = Partner.browse(int(customer_id)).exists() if str(customer_id).isdigit() else Partner
        if not partner or not partner.active or partner.customer_rank <= 0:
            raise Problem(404, "not_found")
        if not isinstance(body, dict) or "base_location" not in body:
            raise Problem.field("base_location")
        loc = body.get("location")
        if not (isinstance(loc, dict) and set(loc) == {"lat", "lon"} and all(
                isinstance(loc[a], (int, float)) and not isinstance(loc[a], bool) and -b <= loc[a] <= b
                for a, b in (("lat", 90), ("lon", 180)))):
            raise Problem.field("location")
        current = self.customer_data(partner, company)
        base, now_loc = body["base_location"], current["location"]
        same = (base is None and now_loc is None) or (
            isinstance(base, dict) and now_loc is not None and abs(base.get("lat", 999) - now_loc["lat"]) <= 1e-6
            and abs(base.get("lon", 999) - now_loc["lon"]) <= 1e-6)
        if not same:
            raise Problem(409, "stale_write", current=current)
        partner.write({"partner_latitude": loc["lat"], "partner_longitude": loc["lon"]})
        return self.customer_data(partner, company)

    def upload(self, driver, client_id, content_type, data):
        with self.env.cr.savepoint():
            return DeliveryService(self).upload(driver, client_id, content_type, data)

    def licence_tenant_key(self):
        """Meta.licence (T9.7.1.10c): main company's Sweaka company code (public), when configured."""
        return self.env["res.company"].sudo().search([], limit=1, order="id").sweaka_licence_tenant_key or None

    def cursor_expired(self, driver, key):
        """Archived records are kept (tombstones stay computable) but cursors older than 400 days → full resync."""
        return key.changed_at < datetime.now(timezone.utc) - timedelta(days=400)

    def find_by_client_id(self, driver, resource, client_id):
        docs = {"stock_losses": ("sweaka.stock.loss", DocService.loss_data),
                "cash_handovers": ("sweaka.cash.handover", DocService.handover_data)}
        if resource in docs:
            model, render = docs[resource]
            rec = self.env[model].sudo().search([("client_id", "=", client_id)], limit=1)
            return (render(rec), rec.driver_id.id == int(driver.id)) if rec else None
        if resource == "returns":
            ref = self.env["account.move"].sudo().search([("sweaka_client_id", "=", client_id)], limit=1)
            return (ReturnService(self).return_data(ref), ref.sweaka_driver_id.id == int(driver.id)) if ref else None
        if resource == "gps_batches":
            batch = self.env["sweaka.driver.gps.batch"].sudo().search([("client_id", "=", client_id)], limit=1)
            return (GpsService.batch_data(batch), batch.driver_id.id == int(driver.id)) if batch else None
        if resource == "payments":
            pay = self.env["account.payment"].sudo().search([("sweaka_client_id", "=", client_id)], limit=1)
            if pay:
                return PaymentService(self).payment_data(pay), pay.sweaka_driver_id.id == int(driver.id)
            return None
        if resource == "orders":
            order = self.env["sale.order"].sudo().search([("sweaka_client_id", "=", client_id),
                                                         ("sweaka_kind", "=", "order")], limit=1)
            return (OrderService(self).order_data(order), order.sweaka_driver_id.id == int(driver.id)) if order else None
        if resource == "sales":
            order = self.env["sale.order"].sudo().search([("sweaka_client_id", "=", client_id),
                                                         ("sweaka_kind", "!=", "order")], limit=1)
            if order:
                return SaleService(self).sale_data(order), order.sweaka_driver_id.id == int(driver.id)
            return None
        if resource == "customers":
            partner = self.env["res.partner"].sudo().with_context(active_test=False).search(
                [("sweaka_client_id", "=", client_id)], limit=1)
            if partner:
                company = self._user(driver).company_id
                visible = partner.active and partner.company_id in (company, self.env["res.company"])
                return self.customer_data(partner, company), bool(visible)
        return None

    def create(self, driver, resource, body):
        handler = {"customers": self._create_customer, "sales": SaleService(self).create,
                   "payments": PaymentService(self).create, "returns": ReturnService(self).create,
                   "gps_batches": GpsService(self).create, "orders": OrderService(self).create,
                   "stock_losses": DocService(self).create_loss,
                   "cash_handovers": DocService(self).create_handover}.get(resource)
        if handler is None:
            raise Problem(404, "capability_disabled")
        with self.env.cr.savepoint():  # a Problem raised mid-way leaves no half-created records
            return handler(driver, body)

    def _create_customer(self, driver, body):
        """CustomerCreate → res.partner (customer_rank 1); zones = partner tags; city_id = existing customer city."""
        company = self._user(driver).company_id
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 256:
            raise Problem.field("name")
        tags = self.env["res.partner.category"].sudo()
        zone_ids = body.get("zone_ids") or []
        zones = tags.browse()
        for i, zid in enumerate(zone_ids):
            tag = tags.browse(int(zid)).exists() if str(zid).isdigit() else tags
            if not tag:
                raise Problem.field(f"zone_ids[{i}]")
            zones |= tag
        if not zones:  # driver's zone(s): tags of the customers already served (keeps the customer visible)
            zones = self.env["res.partner"].sudo().search([("customer_rank", ">", 0)], limit=50).category_id[:1]
        city = body.get("city_id")
        if city is not None and not self.env["res.partner"].sudo().search_count([("city", "=", city)]):
            raise Problem.field("city_id")
        vals = {"name": name.strip(), "phone": body.get("phone") or False, "street": body.get("address") or False,
                "city": city or False, "customer_rank": 1, "company_id": company.id,
                "category_id": [(6, 0, zones.ids)], "sweaka_client_id": body["client_id"]}
        loc = body.get("location")
        if isinstance(loc, dict) and {"lat", "lon"} <= set(loc):
            vals.update(partner_latitude=loc["lat"], partner_longitude=loc["lon"])
        partner = self.env["res.partner"].sudo().create(vals)
        return self.customer_data(partner, company)

    def action(self, driver, path, method, ids, body):
        service = OrderService(self)
        with self.env.cr.savepoint():
            if path.startswith("/customers/"):
                return self._set_location(driver, ids[0], body)
            if path.startswith("/van-loads/"):
                return DocService(self).validate_van_load(driver, ids[0], body)
            if path == "/day-closes":
                return DocService(self).day_close(driver, body)
            if path.startswith("/deliveries/"):
                return DeliveryService(self).event(driver, ids[0], body)
            if path.startswith("/cash-handovers/") and path.endswith("/confirm"):
                return DocService(self).confirm_handover(driver, ids[0], body)
            if method == "PUT" and path.startswith("/orders/"):
                return service.edit(driver, ids[0], body)
            if method == "POST" and path.endswith("/cancel"):
                return service.cancel(driver, ids[0], body)
        raise Problem(404, "capability_disabled")


class OdooState:
    def __init__(self, env):
        self.env = env

    def issue_token(self, driver, device):
        return self.env["sweaka.driver.token"].sudo().issue(int(driver.id), device)

    def token_driver(self, token):
        user = self.env["sweaka.driver.token"].sudo().user_of(token)
        return str(user.id) if user else None

    def revoke_token(self, token):
        self.env["sweaka.driver.token"].sudo().revoke(token)

    def _row(self, driver_id, key):
        return self.env["sweaka.driver.idempotency"].sudo().search(
            [("user_id", "=", int(driver_id)), ("key", "=", key)], limit=1)

    def idem_get(self, driver_id, key):
        row = self._row(driver_id, key)
        if not row:
            return None
        return row.fingerprint, (row.status or None), row.body

    def idem_begin(self, driver_id, key, fingerprint):
        if self._row(driver_id, key):
            return False
        self.env["sweaka.driver.idempotency"].sudo().create(
            {"user_id": int(driver_id), "key": key, "fingerprint": fingerprint, "status": 0})
        return True

    def idem_finish(self, driver_id, key, status, body):
        row = self._row(driver_id, key)
        if status is None:
            row.unlink()
        else:
            row.write({"status": status, "body": body})


def _dec(value):
    text = f"{float(value or 0):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _authenticate(users, db, credential):
    """res.users.authenticate across series → uid (raises AccessDenied).
    19: model method (credential, env) → auth_info; 18: classmethod (db, credential, env) → auth_info;
    17: classmethod (db, login, password, env) → uid."""
    import odoo.release

    major = odoo.release.version_info[0]
    env = {"interactive": False}
    if major >= 19:
        return users.authenticate(credential, env)["uid"]
    if major == 18:
        return type(users).authenticate(db, credential, env)["uid"]
    return type(users).authenticate(db, credential["login"], credential["password"], env)
