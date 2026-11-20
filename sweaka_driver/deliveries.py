"""Contract deliveries (T9.1.4h.8): outgoing pickings of the driver → Delivery; events → picking validation."""
import base64
import hashlib
from datetime import timezone

from .lib.sweaka_connect_core import Problem
from .lib.sweaka_connect_core.feed import iso
from .sales import _when

TRANSITIONS = {"start": ({"assigned", "failed"}, "out_for_delivery"), "deliver": ({"out_for_delivery"}, "delivered"),
               "partial": ({"out_for_delivery"}, "partially_delivered"), "fail": ({"out_for_delivery"}, "failed"),
               "return": ({"failed"}, "returned")}
REASONS = ("absent", "refused", "address_wrong", "damaged", "other")
TYPES = ("image/jpeg", "image/png", "image/webp")


def _utc(dt):
    return iso(dt.replace(tzinfo=timezone.utc)) if dt else None


class DeliveryService:
    def __init__(self, wms):
        self.wms, self.env = wms, wms.env

    # ---- read ----------------------------------------------------------------------------------------------------
    @staticmethod
    def domain(user):
        return [("sweaka_driver_id", "=", user.id), ("picking_type_code", "=", "outgoing"), ("state", "!=", "draft")]

    @staticmethod
    def status(pick):
        if pick.state == "cancel" and pick.sweaka_delivery_status not in ("delivered", "partially_delivered"):
            return "cancelled"
        return pick.sweaka_delivery_status or "assigned"

    @staticmethod
    def lines(pick):
        frozen = pick.sweaka_delivered_json or {}
        out = []
        for move in pick.move_ids.sorted("id"):
            key = str(move.id)
            if key in frozen:
                qty, done = frozen[key]
            elif move.state == "cancel":
                continue  # remainder split off at a partial validation (back to the depot)
            else:
                qty, done = int(move.product_uom_qty), 0
            out.append({"id": key, "product_id": str(move.product_id.id), "name": move.product_id.display_name,
                        "qty": max(int(qty), 1), "delivered_qty": int(done)})
        return out

    @classmethod
    def delivery_data(cls, pick):
        partner = pick.partner_id
        order = pick.sale_id
        cur = (order.currency_id or pick.company_id.currency_id)
        due = order.amount_total - sum(order.invoice_ids.filtered(lambda m: m.state == "posted").mapped(
            lambda m: m.amount_total - m.amount_residual)) if order else 0
        loc = ({"lat": partner.partner_latitude, "lon": partner.partner_longitude}
               if partner.partner_latitude or partner.partner_longitude else None)
        return {
            "id": str(pick.id), "number": pick.name, "status": cls.status(pick),
            "order_id": str(order.id) if order else None,
            "customer_id": str(partner.commercial_partner_id.id) if partner and partner.commercial_partner_id.customer_rank > 0 else None,
            "recipient": {"name": partner.display_name or pick.name, "phone": partner.phone or None,
                          "address": " ".join((partner.contact_address or "").split()) or None, "location": loc},
            "lines": cls.lines(pick) or [{"id": "0", "product_id": None, "name": pick.name, "qty": 1, "delivered_qty": 0}],
            "cod_amount": ({"amount_minor": int(round(due * 10 ** cur.decimal_places)), "currency": cur.name}
                           if order and due > 0 else None),
            "window": None, "sequence": pick.sweaka_sequence or None, "attempts": pick.sweaka_attempts,
            "proof": {"photo_ids": [str(i) for i in pick.sweaka_photo_ids.ids],
                      "signature_id": str(pick.sweaka_signature_id.id) if pick.sweaka_signature_id else None,
                      "recipient_name": pick.sweaka_recipient_name or None},
            "failure_reason": pick.sweaka_failure_reason or None, "delivered_at": _utc(pick.sweaka_delivered_at),
            "updated_at": _utc(pick.write_date),
        }

    def picking(self, driver, delivery_id):
        Pick = self.env["stock.picking"].sudo()
        pick = Pick.browse(int(delivery_id)).exists() if str(delivery_id).isdigit() else Pick
        if not pick or pick.sweaka_driver_id.id != int(driver.id) or pick.picking_type_code != "outgoing":
            raise Problem(404, "not_found")
        return pick

    # ---- uploads -------------------------------------------------------------------------------------------------
    @staticmethod
    def upload_data(rec):
        return {"id": str(rec.id), "client_id": rec.client_id, "sha256": rec.sha256, "size": rec.size,
                "content_type": rec.content_type}

    def _as_driver(self, driver):
        self.env = self.env(user=int(driver.id), su=True)  # auth=none request: stock writes need a real user

    def upload(self, driver, client_id, content_type, data):
        self._as_driver(driver)
        if content_type not in TYPES:
            raise Problem.field("file", "content_type")
        sha = hashlib.sha256(data).hexdigest()
        Upload = self.env["sweaka.driver.upload"].sudo()
        known = Upload.search([("client_id", "=", client_id)], limit=1)
        if known:
            if known.driver_id.id != int(driver.id) or known.sha256 != sha:
                raise Problem(409, "client_id_conflict")
            return 200, self.upload_data(known)
        Att = self.env["ir.attachment"].sudo().with_context(image_no_postprocess=True)  # proof kept as sent
        content = {"raw": data} if "raw" in Att._fields else {"datas": base64.b64encode(data).decode()}
        att = Att.create({"name": f"sweaka-proof-{client_id[:8]}", **content, "mimetype": content_type,
                          "res_model": "sweaka.driver.upload"})
        rec = Upload.create({"client_id": client_id, "driver_id": int(driver.id), "sha256": sha, "size": len(data),
                             "content_type": content_type, "attachment_id": att.id})
        att.res_id = rec.id
        return 201, self.upload_data(rec)

    # ---- events --------------------------------------------------------------------------------------------------
    def _uploads(self, driver, ids, field):
        Upload = self.env["sweaka.driver.upload"].sudo()
        recs = Upload.browse()
        for i, uid in enumerate(ids):
            rec = Upload.browse(int(uid)).exists() if str(uid).isdigit() else Upload
            if not rec or rec.driver_id.id != int(driver.id):
                raise Problem.field(field.format(i=i))
            recs |= rec
        return recs

    def _validate(self, pick, delivered):
        """Ship the delivered quantities; the remainder goes back to the depot (no backorder)."""
        frozen = {}
        for move in pick.move_ids.filtered(lambda m: m.state not in ("cancel", "done")):
            qty = int(move.product_uom_qty)
            done = qty if delivered is None else delivered.get(str(move.id), 0)
            frozen[str(move.id)] = [qty, done]
            move.quantity = done
            move.picked = True
        if not any(done for _, done in frozen.values()):
            raise Problem.field("lines")
        pick.sweaka_delivered_json = frozen
        pick.with_context(skip_backorder=True, cancel_backorder=True, picking_ids_not_to_backorder=pick.ids,
                          skip_sms=True, skip_immediate=True).button_validate()

    def event(self, driver, delivery_id, body):
        self._as_driver(driver)
        pick = self.picking(driver, delivery_id)
        if not isinstance(body, dict):
            raise Problem.field("body")
        cid = body.get("client_id")
        from .lib.sweaka_connect_core.api import _uuid

        if not _uuid(cid):
            raise Problem.field("client_id")
        Event = self.env["sweaka.delivery.event"].sudo()
        known = Event.search([("client_id", "=", str(cid))], limit=1)
        if known:
            if known.driver_id.id != int(driver.id):
                raise Problem(409, "client_id_conflict")
            return 200, self.event_data(known)
        etype = body.get("type")
        if etype not in TRANSITIONS:
            raise Problem.field("type")
        at = _when(body.get("at"), "at")
        proof = body.get("proof") if isinstance(body.get("proof"), dict) else {}
        photos = self._uploads(driver, proof.get("photo_ids") or [], "proof.photo_ids[{i}]")
        sig = self._uploads(driver, [proof["signature_id"]] if proof.get("signature_id") else [], "proof.signature_id")
        need = pick.company_id.sweaka_proof_required or "none"
        if etype in ("deliver", "partial") and ((need in ("photo", "both") and not photos)
                                                or (need in ("signature", "both") and not sig)):
            raise Problem.field("proof")
        delivered = None
        if etype == "partial":
            lines = body.get("lines")
            if not isinstance(lines, list) or not lines:
                raise Problem.field("lines")
            moves = {str(m.id): m for m in pick.move_ids}
            delivered = {}
            for i, ln in enumerate(lines):
                if not isinstance(ln, dict) or str(ln.get("line_id")) not in moves:
                    raise Problem.field(f"lines[{i}].line_id")
                q = ln.get("qty")
                if not isinstance(q, int) or isinstance(q, bool) or not 0 <= q <= moves[str(ln["line_id"])].product_uom_qty:
                    raise Problem.field(f"lines[{i}].qty")
                delivered[str(ln["line_id"])] = q
        reason = body.get("reason")
        if etype == "fail" and reason not in REASONS:
            raise Problem.field("reason")
        loc = body.get("location") if isinstance(body.get("location"), dict) else {}
        stale = bool(pick.sweaka_last_event_at and at < pick.sweaka_last_event_at)
        if not stale:
            allowed, target = TRANSITIONS[etype]
            current = self.status(pick)
            if current not in allowed:
                raise Problem(409, "not_editable", current=self.delivery_data(pick))
            vals = {"sweaka_delivery_status": target, "sweaka_last_event_at": at}
            if etype in ("deliver", "partial"):
                self._validate(pick, delivered)
                vals.update(sweaka_delivered_at=at, sweaka_photo_ids=[(6, 0, photos.ids)],
                            sweaka_signature_id=sig.id or False,
                            sweaka_recipient_name=proof.get("recipient_name") or False)
            if etype == "fail":
                vals.update(sweaka_attempts=pick.sweaka_attempts + 1, sweaka_failure_reason=reason)
            pick.write(vals)
        ev = Event.create({"client_id": str(cid), "picking_id": pick.id, "driver_id": int(driver.id), "type": etype,
                           "at": at, "applied": not stale, "reason": reason if etype == "fail" else False,
                           "note": body.get("note") or False, "latitude": loc.get("lat") or 0.0,
                           "longitude": loc.get("lon") or 0.0})
        return 201, self.event_data(ev)

    def event_data(self, ev):
        return {"id": str(ev.id), "client_id": ev.client_id, "type": ev.type, "at": _utc(ev.at), "applied": ev.applied,
                "delivery": self.delivery_data(ev.picking_id)}
