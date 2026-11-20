"""WSGI wrapper: serve a DriverApi under a mount prefix (stdlib wsgiref-compatible).

An unhandled exception becomes a 500 application/problem+json (internal_error, details only in the log) — done here,
not in DriverApi.handle, so hosts with transactions (Odoo) still see the exception and roll back (T9.5.6).
"""
import json
import logging
from urllib.parse import parse_qs

from .api import Response
from .errors import problem_body

log = logging.getLogger("sweaka_connect_core")


def make_wsgi_app(api, prefix=""):
    def app(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        headers = {k[5:].replace("_", "-"): v for k, v in environ.items() if k.startswith("HTTP_")}
        if environ.get("CONTENT_TYPE"):  # WSGI keeps it out of HTTP_* (multipart /uploads needs the boundary)
            headers["CONTENT-TYPE"] = environ["CONTENT_TYPE"]
        query = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        try:
            resp = api.handle(environ["REQUEST_METHOD"], path, headers, query, body)
        except Exception:  # noqa: BLE001 — last line of defence; never leak internals to the phone
            log.exception("unhandled error on %s %s", environ["REQUEST_METHOD"], path)
            resp = Response(500, problem_body(500, "internal_error"))
        payload = resp.json_bytes()
        out = [("Content-Type", resp.content_type)] if payload else []
        out += [(k, str(v)) for k, v in resp.headers.items()]
        start_response(f"{resp.status} {_reason(resp.status)}", out)
        return [payload]

    return app


def _reason(status):
    return {200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 409: "Conflict", 422: "Unprocessable Entity",
            426: "Upgrade Required", 429: "Too Many Requests", 500: "Internal Server Error"}.get(status, "OK")


__all__ = ["make_wsgi_app", "json"]
