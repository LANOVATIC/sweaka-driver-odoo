"""DriverApi: the contract's HTTP semantics, framework-agnostic (T9.5.4).

Adapters call DriverApi(wms, state).handle(method, path, headers, query, body_bytes) from their web layer
(Odoo controller, WSGI, ...). path is relative to the server base URL and starts with /v1.
"""
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field

from . import feed
from .errors import WWW_AUTHENTICATE, Problem, Queued

API_VERSION = "1.0.0"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
PLATFORMS = ("android", "ios")
# create ops: path → (resource, capability)
CREATES = {
    "/customers": ("customers", "customers_create"), "/orders": ("orders", "orders"),
    "/sales": ("sales", "van_sales"), "/payments": ("payments", "payments"), "/returns": ("returns", "returns"),
    "/stock-losses": ("stock_losses", "stock_loss"), "/gps/batches": ("gps_batches", "gps_tracking"),
    "/cash-handovers": ("cash_handovers", "day_close"),
}
# other contract mutating ops (adapter.action) → capability; online-only ones are refused inside /sync/push
ACTIONS = [
    ("PUT", re.compile(r"^/customers/([^/]+)/location$"), "customers_update_location", False),
    ("PUT", re.compile(r"^/orders/([^/]+)$"), "orders", False),
    ("POST", re.compile(r"^/orders/([^/]+)/cancel$"), "orders", False),
    ("POST", re.compile(r"^/van-loads/([^/]+)/validate$"), "stock_validation", True),
    ("POST", re.compile(r"^/day-closes$"), "day_close", True),
    ("POST", re.compile(r"^/cash-handovers/([^/]+)/confirm$"), "day_close", True),  # T9.1.2c.2 option B
    ("POST", re.compile(r"^/deliveries/([^/]+)/events$"), "deliveries", False),  # T9.1.4h.3 (offline-able)
]
ACTION_STATUS = {"/day-closes": 201}  # a day close creates a record; other actions answer 200
RETRYABLE = {409, 429}
MAX_UPLOAD = 5 * 1024 * 1024


@dataclass
class Response:
    status: int
    body: object = None
    headers: dict = field(default_factory=dict)

    @property
    def content_type(self):
        return "application/problem+json" if self.status >= 400 else "application/json"

    def json_bytes(self):
        return b"" if self.body is None else json.dumps(self.body, separators=(",", ":")).encode()


def _semver(value):
    m = SEMVER.match(value or "")
    return tuple(int(p) for p in m.groups()) if m else None


def _uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


class DriverApi:
    def __init__(self, wms, state, server_kind="connect_core", server_version="0.1.0", min_app_version="1.0.0",
                 extra_routes=None):
        """extra_routes: {(METHOD, path): fn(driver, body) → (status, body)} served after auth, also as
        /sync/push ops (used by a server's test-only kit package; never by shipped plugins themselves)."""
        self.wms, self.state = wms, state
        self.extra_routes = dict(extra_routes or {})
        self.server_kind, self.server_version, self.min_app_version = server_kind, server_version, min_app_version

    # ---- entry point -------------------------------------------------------------------------------------
    def handle(self, method, path, headers=None, query=None, body=b""):
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        try:
            return self._route(method.upper(), path.rstrip("/") or "/", headers, query or {}, body)
        except Problem as p:
            return self._problem(p)

    def _problem(self, p):
        headers = dict(p.headers)
        if p.status == 401:
            headers["WWW-Authenticate"] = WWW_AUTHENTICATE
        return Response(p.status, p.body(), headers)

    def _route(self, method, path, headers, query, body):
        if not path.startswith("/v1/") and path != "/v1":
            raise Problem(404, "not_found")
        path = path[3:] or "/"
        if (method, path) == ("GET", "/meta"):
            return Response(200, self.meta())
        sent = _semver(headers.get("x-app-version"))
        if sent is not None and sent < _semver(self.min_app_version):
            raise Problem(426, "app_version_unsupported", min_app_version=self.min_app_version)
        if (method, path) == ("POST", "/auth/login"):
            return self.login(self._json(body))
        token, driver = self._auth(headers)
        if (method, path) == ("POST", "/auth/logout"):
            self.state.revoke_token(token)
            return Response(204)
        if (method, path) == ("GET", "/me"):
            return Response(200, self.wms.profile(driver))
        if (method, path) == ("GET", "/changes"):
            return Response(200, self.changes(driver, query))
        if (method, path) in self.extra_routes:  # kit routes: plain calls (no Idempotency-Key); as push ops they replay
            status, out = self.extra_routes[(method, path)](driver, self._json(body))
            return Response(status, out)
        if (method, path) == ("POST", "/uploads"):  # T9.1.4h.1: multipart, never JSON
            return self._idempotent(driver, headers, method, path, body,
                                    lambda: self.upload(driver, headers, body))
        if (method, path) == ("POST", "/sync/push"):
            return self._idempotent(driver, headers, method, path, body,
                                    lambda: self.push(driver, self._json(body)), batch=True)
        return self._idempotent(driver, headers, method, path, body,
                                lambda: self._op(driver, method, path, self._json(body)))

    # ---- ops ---------------------------------------------------------------------------------------------
    def meta(self):
        caps = self.wms.capabilities()
        out = {"api_version": API_VERSION, "server_kind": self.server_kind, "server_version": self.server_version,
               "min_app_version": self.min_app_version, "capabilities": caps}
        key = getattr(self.wms, "licence_tenant_key", None)  # optional adapter hook (T9.7.1.10c)
        key = key() if callable(key) else None
        if key:
            out["licence"] = {"tenant_key": key}
        return out

    def login(self, data):
        if not isinstance(data, dict):
            raise Problem.field("body")
        fields = [f for f in ("username", "password") if not isinstance(data.get(f), str) or not data.get(f)]
        device = data.get("device")
        if not isinstance(device, dict):
            fields.append("device")
        else:
            if not _uuid(device.get("device_id")):
                fields.append("device.device_id")
            if device.get("platform") not in PLATFORMS:
                fields.append("device.platform")
            if _semver(device.get("app_version")) is None:
                fields.append("device.app_version")
        if fields:
            raise Problem(400, "validation_failed", fields=[{"field": f, "code": "invalid"} for f in fields])
        driver = self.wms.authenticate(data["username"], data["password"])
        if driver is None:
            raise Problem(401, "invalid_credentials")
        token = self.state.issue_token(driver, device)
        return Response(200, {"token": token, "expires_at": None, "driver": {"id": driver.id, "name": driver.name}})

    def changes(self, driver, query):
        raw = str(query.get("limit", "50"))
        if not raw.isdigit() or not 1 <= int(raw) <= 200:
            raise Problem.field("limit")
        since = query.get("since")
        if since is not None and (not since or len(since) > 512):
            raise Problem(400, "invalid_cursor")
        return feed.page(self.wms, driver, since, int(raw))

    def _op(self, driver, method, path, body):
        if (method, path) in self.extra_routes:
            return self.extra_routes[(method, path)](driver, body)
        caps = self.wms.capabilities()
        if method == "POST" and path in CREATES:
            resource, cap = CREATES[path]
            if not caps.get(cap):
                raise Problem(404, "capability_disabled")
            return self.create(driver, resource, body)
        for m, rx, cap, _online in ACTIONS:
            match = rx.match(path)
            if m == method and match:
                if not caps.get(cap):
                    raise Problem(404, "capability_disabled")
                out = self.wms.action(driver, path, method, match.groups(), body)
                if isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], int):
                    return out  # adapter-chosen status (delivery events: 201 new / 200 known client_id)
                return ACTION_STATUS.get(path, 200), out
        raise Problem(404, "not_found")

    def create(self, driver, resource, body):
        """Client ids rule: new client_id → 201; known + visible → 200 stored (body ignored); else 409."""
        if not isinstance(body, dict):
            raise Problem.field("body")
        if not _uuid(body.get("client_id")):
            raise Problem.field("client_id")
        try:
            known = self.wms.find_by_client_id(driver, resource, str(body["client_id"]))
            if known is not None:
                record, visible = known
                if not visible:
                    raise Problem(409, "client_id_conflict")
                return 200, record
            return 201, self.wms.create(driver, resource, body)
        except Queued as q:  # server-side write queue (T9.8.7): accepted, applied later
            return 202, q.body()

    def upload(self, driver, headers, raw):
        """POST /uploads (cap deliveries): multipart client_id + file → adapter.upload(driver, client_id, type, data)."""
        if not self.wms.capabilities().get("deliveries") or not hasattr(self.wms, "upload"):
            raise Problem(404, "capability_disabled")
        from .multipart import parse

        if len(raw or b"") > MAX_UPLOAD + 4096:
            raise Problem(413, "upload_too_large")
        parts = parse(headers.get("content-type", ""), raw or b"")
        client_id = (parts.get("client_id") or ("", b""))[1].decode(errors="replace").strip()
        if not _uuid(client_id):
            raise Problem.field("client_id")
        ctype, data = parts.get("file") or ("", b"")
        if not data:
            raise Problem.field("file")
        if len(data) > MAX_UPLOAD:
            raise Problem(413, "upload_too_large")
        if ctype not in ("image/jpeg", "image/png", "image/webp"):
            raise Problem.field("file", "content_type")
        return self.wms.upload(driver, client_id, ctype, data)

    # ---- sync push -----------------------------------------------------------------------------------------
    def push(self, driver, data):
        if not isinstance(data, dict) or set(data) != {"ops"}:
            raise Problem.field("body")
        ops = data["ops"]
        if not isinstance(ops, list) or not ops:
            raise Problem.field("ops")
        if len(ops) > 100:
            raise Problem.field("ops", "too_many")
        seen = set()
        for i, o in enumerate(ops):
            ok = (isinstance(o, dict) and {"op_id", "method", "path"} <= set(o) and not set(o) - {
                "op_id", "method", "path", "body"} and _uuid(o["op_id"])
                  and o["method"] in ("POST", "PUT", "PATCH", "DELETE") and isinstance(o["path"], str)
                  and re.match(r"^/(?!sync/)[A-Za-z0-9._~/-]+$", o["path"]) and len(o["path"]) <= 512)
            if not ok:
                raise Problem.field(f"ops[{i}]")
            if o["op_id"] in seen:
                raise Problem.field(f"ops[{i}].op_id", "duplicate")
            seen.add(o["op_id"])
        results = []
        for o in ops:
            status, out, replayed = self._push_op(driver, o)
            result = {"op_id": o["op_id"], "status": status, "replayed": replayed}
            result["problem" if status >= 400 else "body"] = out
            results.append(result)
            if status in RETRYABLE or status >= 500:
                break
        return 200, {"results": results}

    def _push_op(self, driver, o):
        path = o["path"]
        if path == "/uploads":  # x-not-in-push: multipart only (T9.1.4h.1)
            return 400, Problem.field("path", "not_in_push").body(), False
        for m, rx, _cap, online in ACTIONS:
            if online and m == o["method"] and rx.match(path):
                return 400, Problem.field("path", "online_only").body(), False
        raw = json.dumps(o.get("body")).encode()
        resp, replayed = self._run_once(driver, o["op_id"], o["method"], "/v1" + path, raw,
                                        lambda: self._op(driver, o["method"], path, o.get("body")))
        return resp.status, resp.body, replayed

    # ---- idempotency ---------------------------------------------------------------------------------------
    def _idempotent(self, driver, headers, method, path, body, execute, batch=False):
        key = headers.get("idempotency-key")
        if not _uuid(key):
            raise Problem.field("Idempotency-Key")
        resp, replayed = self._run_once(driver, ("batch:" if batch else "") + str(uuid.UUID(key)), method,
                                        "/v1" + path, body, execute)
        if replayed:
            resp.headers["Idempotent-Replayed"] = "true"
        return resp

    def _run_once(self, driver, key, method, full_path, body, execute):
        """→ (Response, replayed). Final outcomes stored per (driver, key); 409/429/5xx never stored."""
        if isinstance(body, (bytes, bytearray)) and full_path.endswith("/uploads"):
            canonical = hashlib.sha256(bytes(body)).hexdigest()  # multipart: fingerprint the raw bytes
        else:
            canonical = json.dumps(self._json(body), sort_keys=True) if body else ""
        fp = hashlib.sha256(f"{method} {full_path} {canonical}".encode()).hexdigest()
        stored = self.state.idem_get(driver.id, key)
        if stored is not None:
            if stored[0] != fp:
                return self._problem(Problem(422, "idempotency_key_reused")), False
            if stored[1] is None:
                return self._problem(Problem(409, "idempotency_in_progress", headers={"Retry-After": "2"},
                                             retry_after_seconds=2)), False
            return Response(stored[1], stored[2]), True
        if not self.state.idem_begin(driver.id, key, fp):
            return self._problem(Problem(409, "idempotency_in_progress", headers={"Retry-After": "2"},
                                         retry_after_seconds=2)), False
        try:
            status, out = execute()
            resp = Response(status, out)
        except Problem as p:
            resp = self._problem(p)
        except Exception:
            self.state.idem_finish(driver.id, key, None, None)
            raise
        final = resp.status < 500 and resp.status not in RETRYABLE
        self.state.idem_finish(driver.id, key, resp.status if final else None, resp.body if final else None)
        return resp, False

    # ---- helpers -------------------------------------------------------------------------------------------
    def _auth(self, headers):
        scheme, _, token = (headers.get("authorization") or "").partition(" ")
        token = token.strip()
        driver_id = self.state.token_driver(token) if scheme.lower() == "bearer" and token else None
        driver = self.wms.driver(driver_id) if driver_id else None
        if driver is None:
            raise Problem(401, "unauthenticated")
        return token, driver

    @staticmethod
    def _json(body):
        if body in (None, b"", ""):
            return None
        if isinstance(body, (dict, list)):
            return body
        try:
            return json.loads(body)
        except ValueError:
            raise Problem.field("body") from None
