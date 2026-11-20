"""problem+json errors (contract CONVENTIONS "Errors")."""

TITLES = {
    "validation_failed": "Invalid request", "unauthenticated": "Authentication required",
    "invalid_credentials": "Wrong username or password", "forbidden": "Not allowed", "not_found": "Not found",
    "invalid_cursor": "Invalid cursor", "capability_disabled": "Feature disabled on this server",
    "app_version_unsupported": "App update required", "rate_limited": "Too many requests",
    "internal_error": "Internal error", "upstream_unavailable": "WMS unavailable",
    "idempotency_key_reused": "Idempotency-Key reused with another request",
    "idempotency_in_progress": "Same request still running", "client_id_conflict": "client_id belongs to another record",
    "resync_required": "Full resync required", "stale_write": "Record changed since you read it",
    "not_editable": "Record can no longer be changed",
}
WWW_AUTHENTICATE = 'Bearer realm="sweaka-driver"'


def problem_body(status, code, fields=None, **extra):
    body = {"type": "https://sweaka.com/problems/" + code, "title": TITLES.get(code, code), "status": status,
            "code": code}
    if fields:
        body["fields"] = fields
    body.update(extra)
    return body


class Problem(Exception):
    """Raise from adapters or core: becomes an application/problem+json response."""

    def __init__(self, status, code, fields=None, headers=None, **extra):
        super().__init__(code)
        self.status, self.code, self.fields, self.headers, self.extra = status, code, fields, headers or {}, extra

    @classmethod
    def field(cls, field, code="invalid"):
        return cls(400, "validation_failed", fields=[{"field": field, "code": code}])

    def body(self):
        return problem_body(self.status, self.code, self.fields, **self.extra)


class Queued(Exception):
    """Raise from a create (or find_by_client_id) when the write was queued server-side (T9.8.7) → 202 Queued."""

    def __init__(self, client_id):
        super().__init__(client_id)
        self.client_id = client_id

    def body(self):
        return {"client_id": self.client_id, "status": "queued"}
