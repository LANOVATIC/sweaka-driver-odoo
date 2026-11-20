"""Core StatePort storage: driver tokens (hashed, per device) + idempotency outcomes (T9.6.2)."""
import hashlib
import secrets

from odoo import api, fields, models


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class SweakaDriverToken(models.Model):
    _name = "sweaka.driver.token"
    _description = "Sweaka driver app token (hashed)"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    key_hash = fields.Char(required=True, index=True)
    device_id = fields.Char(required=True)
    platform = fields.Char()
    app_version = fields.Char()
    revoked = fields.Boolean(default=False, index=True)

    if hasattr(models, "Constraint"):  # Odoo 19+
        _key_hash_uniq = models.Constraint("unique(key_hash)", "Token hash must be unique.")
    else:  # 17 / 18
        _sql_constraints = [("key_hash_uniq", "unique(key_hash)", "Token hash must be unique.")]

    @api.model
    def issue(self, user_id, device):
        self.search([("user_id", "=", user_id), ("device_id", "=", device["device_id"]),
                     ("revoked", "=", False)]).write({"revoked": True})
        raw = secrets.token_urlsafe(32)
        self.create({"user_id": user_id, "key_hash": _hash(raw), "device_id": device["device_id"],
                     "platform": device.get("platform"), "app_version": device.get("app_version")})
        return raw

    @api.model
    def user_of(self, raw):
        token = self.search([("key_hash", "=", _hash(raw)), ("revoked", "=", False)], limit=1)
        return token.user_id if token and token.user_id.active else None

    @api.model
    def revoke(self, raw):
        self.search([("key_hash", "=", _hash(raw))]).write({"revoked": True})


class SweakaDriverIdempotency(models.Model):
    _name = "sweaka.driver.idempotency"
    _description = "Sweaka driver API idempotency outcome"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    key = fields.Char(required=True, index=True)
    fingerprint = fields.Char(required=True)
    status = fields.Integer()  # 0 = in progress
    body = fields.Json()

    if hasattr(models, "Constraint"):
        _user_key_uniq = models.Constraint("unique(user_id, key)", "One outcome per key.")
    else:
        _sql_constraints = [("user_key_uniq", "unique(user_id, key)", "One outcome per key.")]
