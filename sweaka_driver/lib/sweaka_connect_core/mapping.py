"""Per-tenant mapping config as data (T9.5.2): how one WMS tenant's records map to the contract.

JSON/dict shape (validated by TenantConfig.from_dict):
{
  "tenant_id": "acme", "currency": "DZD",
  "capabilities": {"van_sales": true, "orders": false},                # overrides of adapter defaults
  "status_maps": {"order": {"draft": "requested", "sale": "approved", "done": "delivered", "cancel": "cancelled"}},
  "field_maps": {"customer": {"phone": "mobile", "name": "display_name"}},   # contract field → WMS field
  "van_locations": {"<driver id>": "<WMS stock location id>"},
  "price_list": "<WMS pricelist id for drivers>"
}
"""
from dataclasses import dataclass, field
from typing import Optional

from .errors import Problem

CONTRACT_STATUSES = {
    "order": {"requested", "approved", "delivered", "cancelled"},
    "sale": {"completed", "cancelled"}, "payment": {"completed", "cancelled"},
    "return": {"completed", "cancelled"}, "stock_loss": {"pending", "validated", "rejected"},
    "cash_handover": {"pending", "validated", "rejected"}, "van_load": {"pending", "validated"},
}
CAPABILITIES = {"customers_create", "customers_update_location", "orders", "deliveries", "van_sales", "payments",
                "returns", "stock_loss", "van_stock", "stock_validation", "day_close", "gps_tracking"}


class ConfigError(ValueError):
    pass


@dataclass
class TenantConfig:
    tenant_id: str
    currency: str
    capabilities: dict = field(default_factory=dict)
    status_maps: dict = field(default_factory=dict)
    field_maps: dict = field(default_factory=dict)
    van_locations: dict = field(default_factory=dict)
    price_list: Optional[str] = None

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            raise ConfigError("config must be an object")
        unknown = set(raw) - {"tenant_id", "currency", "capabilities", "status_maps", "field_maps",
                              "van_locations", "price_list"}
        if unknown:
            raise ConfigError(f"unknown keys {sorted(unknown)}")
        tenant, currency = raw.get("tenant_id"), raw.get("currency")
        if not isinstance(tenant, str) or not tenant:
            raise ConfigError("tenant_id required")
        if not (isinstance(currency, str) and len(currency) == 3 and currency.isupper()):
            raise ConfigError("currency must be ISO 4217 (e.g. DZD)")
        caps = raw.get("capabilities") or {}
        bad = [k for k, v in caps.items() if k not in CAPABILITIES or not isinstance(v, bool)]
        if bad:
            raise ConfigError(f"bad capabilities {bad}")
        statuses = raw.get("status_maps") or {}
        for resource, mapping in statuses.items():
            allowed = CONTRACT_STATUSES.get(resource)
            if allowed is None:
                raise ConfigError(f"status_maps: unknown resource {resource}")
            wrong = sorted({v for v in mapping.values()} - allowed)
            if wrong:
                raise ConfigError(f"status_maps.{resource}: {wrong} not in {sorted(allowed)}")
        return cls(tenant, currency, dict(caps), {k: dict(v) for k, v in statuses.items()},
                   {k: dict(v) for k, v in (raw.get("field_maps") or {}).items()},
                   dict(raw.get("van_locations") or {}), raw.get("price_list"))

    def status(self, resource, wms_status):
        """WMS status → contract status; unmapped → 500-class adapter error (config must cover every status)."""
        try:
            return self.status_maps[resource][wms_status]
        except KeyError:
            raise Problem(500, "internal_error", detail=f"unmapped {resource} status {wms_status!r}") from None

    def wms_field(self, resource, contract_field):
        return self.field_maps.get(resource, {}).get(contract_field, contract_field)

    def van_location(self, driver_id):
        location = self.van_locations.get(driver_id)
        if location is None:
            raise Problem(403, "forbidden", detail="driver has no van location configured")
        return location

    def capabilities_over(self, defaults):
        return {cap: bool(self.capabilities.get(cap, defaults.get(cap, False))) for cap in sorted(CAPABILITIES)}
