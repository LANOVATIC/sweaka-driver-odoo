"""Stock losses, cash handovers (offline-first creates) + van-load validate / day close (online-only, handover code)
on Odoo (T9.6.12, T9.6.13)."""
from datetime import datetime, timedelta, timezone

from odoo import SUPERUSER_ID, api, fields

from .lib.sweaka_connect_core import Problem
from .lib.sweaka_connect_core.feed import iso
from .models.res_users import _new_code
from .sales import _money_in, _when

LOSS_REASONS = ("damaged", "lost", "expired", "other")
LOCK_AFTER, LOCK_WINDOW = 5, timedelta(minutes=15)


def _to_utc(dt):
    return iso(dt.replace(tzinfo=timezone.utc)) if dt else None


def check_code(user, code):
    """One wrong-code bucket per driver (van-load validate + day close): ≥5 wrong in 15 min → 429 (also for the right
    code); success resets + rotates the code."""
    now = fields.Datetime.now()
    if user.sweaka_code_window and now - user.sweaka_code_window > LOCK_WINDOW:
        user.write({"sweaka_code_wrong": 0, "sweaka_code_window": False})
    if user.sweaka_code_wrong >= LOCK_AFTER:
        wait = int((user.sweaka_code_window + LOCK_WINDOW - now).total_seconds()) + 1
        raise Problem(429, "rate_limited", headers={"Retry-After": str(max(wait, 1))}, retry_after_seconds=max(wait, 1))
    if not isinstance(code, str) or code != user.sweaka_handover_code:
        # the wrong attempt must count although this request is rolled back → own short transaction
        with user.env.registry.cursor() as cr:
            other = api.Environment(cr, SUPERUSER_ID, {})["res.users"].browse(user.id)
            other.write({"sweaka_code_wrong": other.sweaka_code_wrong + 1,
                         "sweaka_code_window": other.sweaka_code_window or now})
        raise Problem.field("code")
    user.write({"sweaka_code_wrong": 0, "sweaka_code_window": False, "sweaka_handover_code": _new_code()})


def _loc(body):
    loc = body.get("location") if isinstance(body.get("location"), dict) else {}
    return {"latitude": loc.get("lat") or 0.0, "longitude": loc.get("lon") or 0.0}


def _point(rec):
    return {"lat": rec.latitude, "lon": rec.longitude} if rec.latitude or rec.longitude else None


class DocService:
    def __init__(self, wms):
        self.wms, self.env = wms, wms.env

    def _as(self, driver):
        user = self.wms._user(driver)
        self.env = self.env(user=user.id, su=True)
        return user

    # ---- stock losses -------------------------------------------------------------------------------------------
    def create_loss(self, driver, body):
        user = self._as(driver)
        if body.get("reason") not in LOSS_REASONS:
            raise Problem.field("reason")
        lines = body.get("lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= 500:
            raise Problem.field("lines")
        Product, rows, seen = self.env["product.product"], [], set()
        for i, line in enumerate(lines):
            pid = str(line.get("product_id")) if isinstance(line, dict) else ""
            product = Product.browse(int(pid)).exists() if pid.isdigit() else Product
            qty = line.get("qty") if isinstance(line, dict) else None
            if not product or not product.active or product.id in seen:
                raise Problem.field(f"lines[{i}].product_id")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                raise Problem.field(f"lines[{i}].qty")
            seen.add(product.id)
            rows.append((0, 0, {"product_id": product.id, "qty": qty}))  # above van stock accepted (back office)
        loss = self.env["sweaka.stock.loss"].create({
            "client_id": body["client_id"], "driver_id": user.id, "reason": body["reason"], "note": body.get("note"),
            "declared_at": _when(body.get("declared_at"), "declared_at"), "line_ids": rows, **_loc(body)})
        return self.loss_data(loss)

    @staticmethod
    def loss_data(loss):
        return {"id": str(loss.id), "client_id": loss.client_id or None, "number": loss.name, "status": loss.state,
                "reason": loss.reason, "lines": [{"product_id": str(l.product_id.id), "product_name": l.product_id.display_name,
                                                  "qty": l.qty} for l in loss.line_ids],
                "location": _point(loss), "note": loss.note or None, "declared_at": _to_utc(loss.declared_at),
                "validated_at": _to_utc(loss.validated_at), "reject_reason": loss.reject_reason or None,
                "rejected_at": _to_utc(loss.rejected_at), "created_at": _to_utc(loss.create_date),
                "updated_at": _to_utc(loss.write_date)}

    # ---- cash handovers -----------------------------------------------------------------------------------------
    def create_handover(self, driver, body):
        user = self._as(driver)
        cur = user.company_id.currency_id
        amount = _money_in(body.get("amount"), "amount", cur, minimum=1)
        note = body.get("note")
        if note is not None and not (isinstance(note, str) and len(note) <= 1000):
            raise Problem.field("note")
        rec = self.env["sweaka.cash.handover"].create({
            "client_id": body["client_id"], "driver_id": user.id, "amount": amount, "currency_id": cur.id,
            "note": note, "declared_at": _when(body.get("declared_at"), "declared_at"), **_loc(body)})
        return self.handover_data(rec)

    @staticmethod
    def handover_data(rec):
        cur = rec.currency_id
        return {"id": str(rec.id), "client_id": rec.client_id or None, "number": rec.name, "status": rec.state,
                "amount": {"amount_minor": int(round(rec.amount * 10 ** cur.decimal_places)), "currency": cur.name},
                "location": _point(rec), "note": rec.note or None, "declared_at": _to_utc(rec.declared_at),
                "validated_at": _to_utc(rec.validated_at), "reject_reason": rec.reject_reason or None,
                "rejected_at": _to_utc(rec.rejected_at), "created_at": _to_utc(rec.create_date),
                "updated_at": _to_utc(rec.write_date)}

    def confirm_handover(self, driver, handover_id, body):
        """T9.1.2c.2 option B: driver types the code the back office shows → handover validated (cash leaves driver)."""
        user = self._as(driver)
        if not isinstance(body, dict):
            raise Problem.field("body")
        Handover = self.env["sweaka.cash.handover"]
        rec = Handover.browse(int(handover_id)).exists() if str(handover_id).isdigit() else Handover
        if not rec or rec.driver_id.id != user.id:
            raise Problem(404, "not_found")
        if rec.state == "validated":
            return self.handover_data(rec)  # already validated: code not checked, cash not moved twice
        if rec.state == "rejected":
            raise Problem(409, "not_editable")
        base = body.get("base_updated_at")
        try:
            base_dt = datetime.fromisoformat(str(base).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            raise Problem.field("base_updated_at") from None
        if base_dt.replace(tzinfo=None) != rec.write_date:
            raise Problem(409, "stale_write", current=self.handover_data(rec))
        check_code(user, body.get("code"))
        rec.write({"state": "validated", "validated_at": fields.Datetime.now()})
        return self.handover_data(rec)

    # ---- van loads (online-only) ----------------------------------------------------------------------------------
    def van_loads(self, user):
        van = user.sweaka_van_location_id
        if not van:
            return self.env["stock.picking"]
        return self.env["stock.picking"].search([("location_dest_id", "=", van.id), ("picking_type_code", "=", "internal"),
                                                ("state", "not in", ("draft", "cancel"))])

    @staticmethod
    def van_load_data(picking):
        moves = picking.move_ids.filtered(lambda m: m.state != "cancel")
        return {"id": str(picking.id), "status": "validated" if picking.state == "done" else "pending",
                "lines": [{"product_id": str(m.product_id.id), "qty": int(m.product_uom_qty)} for m in moves],
                "note": str(picking.note or "") or None, "created_at": _to_utc(picking.create_date),
                "validated_at": _to_utc(picking.date_done) if picking.state == "done" else None,
                "updated_at": _to_utc(picking.write_date)}

    def validate_van_load(self, driver, load_id, body):
        user = self._as(driver)
        if not isinstance(body, dict):
            raise Problem.field("body")
        picking = self.env["stock.picking"].browse(int(load_id)).exists() if str(load_id).isdigit() else None
        if not picking or picking not in self.van_loads(user):
            raise Problem(404, "not_found")
        if picking.state == "done":
            return self.van_load_data(picking)  # already validated: code not checked, nothing re-applied
        base = body.get("base_updated_at")
        if base is not None:
            try:
                base_dt = datetime.fromisoformat(str(base).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                raise Problem.field("base_updated_at") from None
            if base_dt.replace(tzinfo=None) != picking.write_date:
                raise Problem(409, "stale_write", current=self.van_load_data(picking))
        check_code(user, body.get("code"))
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.with_context(skip_backorder=True, skip_sms=True, skip_immediate=True).button_validate()
        return self.van_load_data(picking)

    # ---- day close (online-only) ------------------------------------------------------------------------------------
    def day_close(self, driver, body):
        user = self._as(driver)
        if not isinstance(body, dict):
            raise Problem.field("body")
        note = body.get("note")
        if note is not None and not (isinstance(note, str) and len(note) <= 1000):
            raise Problem.field("note")
        check_code(user, body.get("code"))
        van = user.sweaka_van_location_id
        quants = self.env["stock.quant"].search([("location_id", "=", van.id)]) if van else self.env["stock.quant"]
        lines = {}
        for q in quants:
            entry = lines.setdefault(q.product_id.id, {"product_id": str(q.product_id.id),
                                                       "product_name": q.product_id.display_name, "qty": 0})
            entry["qty"] += int(q.quantity)
        count = self.env["sweaka.day.close"].search_count([("driver_id", "=", user.id)])
        rec = self.env["sweaka.day.close"].create({"name": str(count + 1), "driver_id": user.id,
                                                   "note": note, "lines_json": list(lines.values())})
        return {"id": str(rec.id), "number": count + 1, "lines": rec.lines_json, "note": rec.note or None,  # number = per-driver integer
                "closed_at": _to_utc(rec.create_date), "created_at": _to_utc(rec.create_date)}
