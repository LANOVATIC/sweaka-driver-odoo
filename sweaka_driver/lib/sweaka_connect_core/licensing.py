"""Licensing client for plugins (T9.7.1.11): push the full driver list to Sweaka licensing (T9.7.1.2).

PUT {base_url}/api/v1/licensing/plugin/drivers  headers X-Sweaka-Tenant / X-Sweaka-Plugin-Secret
body {"drivers": [{"external_id", "name", "active"}]} — the list is FULL: drivers missing from it are deactivated
(= stop being billed). stdlib only (vendored into plugins).
"""
import json
import urllib.error
import urllib.request

DEFAULT_URL = "https://api.sweaka.com"
MAX_DRIVERS = 5000


class LicensingError(Exception):
    def __init__(self, status, code, detail=""):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


def driver_payload(drivers):
    """[(external_id, name, active)] → validated body (unique ids, ≤ 5000)."""
    out, seen = [], set()
    for ext_id, name, active in drivers:
        ext_id = str(ext_id)
        if not ext_id or len(ext_id) > 128 or ext_id in seen:
            raise ValueError(f"bad or duplicate driver id {ext_id!r}")
        seen.add(ext_id)
        out.append({"external_id": ext_id, "name": (name or "")[:255], "active": bool(active)})
    if len(out) > MAX_DRIVERS:
        raise ValueError("too many drivers for one sync")
    return {"drivers": out}


def push_drivers(tenant_key, secret, drivers, base_url=DEFAULT_URL, timeout=20, opener=urllib.request.urlopen):
    """→ server summary {seats, drivers, created, updated, deactivated, licensed}; LicensingError on refusal."""
    if not tenant_key or not secret:
        raise LicensingError(0, "not_configured", "tenant key / plugin secret missing")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/licensing/plugin/drivers/", method="PUT",
        data=json.dumps(driver_payload(drivers)).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "X-Sweaka-Tenant": tenant_key, "X-Sweaka-Plugin-Secret": secret})
    try:
        with opener(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        raise LicensingError(exc.code, body.get("code", "http_error"), body.get("detail", "")) from None
    except (urllib.error.URLError, OSError) as exc:
        raise LicensingError(0, "unreachable", str(exc)) from None


def request_sync_push(tenant_key, secret, driver_ids=None, base_url=DEFAULT_URL, timeout=10,
                      opener=urllib.request.urlopen):
    """T9.7.2: ask Sweaka to wake drivers' phones ("sync now", no data). driver_ids None = all drivers.
    → {"devices": n}; LicensingError on refusal (402 = no active subscription)."""
    if not tenant_key or not secret:
        raise LicensingError(0, "not_configured", "tenant key / plugin secret missing")
    ids = None if driver_ids is None else sorted({str(i) for i in driver_ids})
    if ids is not None and not 1 <= len(ids) <= 500:
        raise ValueError("1-500 driver ids per call")
    body = {"all": True} if ids is None else {"driver_external_ids": ids}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/licensing/plugin/sync-push/", method="POST", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "X-Sweaka-Tenant": tenant_key, "X-Sweaka-Plugin-Secret": secret})
    try:
        with opener(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except ValueError:
            data = {}
        raise LicensingError(exc.code, data.get("code", "http_error"), data.get("detail", "")) from None
    except (urllib.error.URLError, OSError) as exc:
        raise LicensingError(0, "unreachable", str(exc)) from None


def push_attention(tenant_key, secret, items, base_url=DEFAULT_URL, timeout=20, opener=urllib.request.urlopen):
    """T9.8.2c: WMS-refused driver writes → licence portal. items = [{client_id, resource, driver_external_id,
    reason_code, reason, rejected_at}] (upsert by client_id; resend is harmless)."""
    if not tenant_key or not secret:
        raise LicensingError(0, "not_configured", "tenant key / plugin secret missing")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/licensing/plugin/attention/", method="POST",
        data=json.dumps({"items": list(items)[:1000]}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "X-Sweaka-Tenant": tenant_key, "X-Sweaka-Plugin-Secret": secret})
    try:
        with opener(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except ValueError:
            data = {}
        raise LicensingError(exc.code, data.get("code", "http_error"), data.get("detail", "")) from None
    except (urllib.error.URLError, OSError) as exc:
        raise LicensingError(0, "unreachable", str(exc)) from None
