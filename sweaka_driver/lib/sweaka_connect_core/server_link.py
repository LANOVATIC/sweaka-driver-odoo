"""ServerLink QR payload (contract schema ServerLink, T9.1.2d.1): what back offices show for drivers to scan."""
import json
import re

BASE_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%!&()*+,;=:-]+)*$")
VERSION_SUFFIX = re.compile(r"/v[0-9]+$")


def server_link(base_url, tenant, name):
    """→ compact JSON for the QR; ValueError with a human reason when the server can't be reached by the app."""
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("the server must be reachable over https:// (set the public URL)")
    if len(base_url) > 256 or not BASE_URL.match(base_url) or VERSION_SUFFIX.search(base_url):
        raise ValueError(f"invalid public URL {base_url!r}")
    tenant, name = str(tenant or ""), (name or "").strip()[:64]
    if not tenant or len(tenant) > 128 or not name:
        raise ValueError("tenant id and company name are required")
    payload = {"type": "sweaka_driver_server", "v": 1, "base_url": base_url, "tenant": tenant, "name": name}
    out = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(out.encode()) > 512:
        raise ValueError("link too long for a QR code")
    return out
