"""Mount the Driver API contract at /sweaka/driver/v1/ (server base URL = https://<odoo>/sweaka/driver)."""
import json
import logging

from odoo import http
from odoo.http import request

from ..adapter import OdooState, OdooWms
from ..lib.sweaka_connect_core import DriverApi
from ..lib.sweaka_connect_core.errors import problem_body

PREFIX = "/sweaka/driver"


def _raw_body(req):
    """Werkzeug may already have parsed a multipart body (stream consumed): rebuild it for the core (POST /uploads)."""
    data = req.get_data()
    ctype = req.headers.get("Content-Type", "")
    if data or not ctype.lower().startswith("multipart/form-data"):
        return data, ctype
    boundary = "sweaka-" + __import__("uuid").uuid4().hex
    out = b""
    for name, value in req.form.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n').encode() + value.encode() + b"\r\n"
    for name, f in req.files.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{f.filename or name}"\r\n'
                f"Content-Type: {f.mimetype or 'application/octet-stream'}\r\n\r\n").encode() + f.read() + b"\r\n"
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"
_logger = logging.getLogger(__name__)


class SweakaDriverController(http.Controller):
    @http.route([PREFIX + "/v1/<path:rest>"], type="http", auth="none", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                csrf=False, save_session=False)
    def driver_api(self, rest, **query):
        req = request.httprequest
        env = request.env  # auth=none: sudo() inside the adapter; one transaction per request
        wms = OdooWms(env)
        # optional test-only kit package (sweaka_driver_test, never shipped) registers extra routes
        kit = env.registry.get("sweaka.driver.kit")
        extra = env["sweaka.driver.kit"].sudo().routes(wms) if kit is not None else None
        api = DriverApi(wms, OdooState(env), server_kind="odoo",
                        server_version=__import__("odoo").release.version, extra_routes=extra)
        try:
            raw, ctype = _raw_body(req)
            headers = {**dict(req.headers), "Content-Type": ctype} if ctype else dict(req.headers)
            resp = api.handle(req.method, "/v1/" + rest, headers, dict(req.args), raw)
        except Exception:  # never an HTML error page for the app
            _logger.exception("sweaka driver api %s %s failed", req.method, rest)
            request.env.cr.rollback()
            body = json.dumps(problem_body(500, "internal_error"))
            return request.make_response(body, headers=[("Content-Type", "application/problem+json")], status=500)
        headers = [(k, str(v)) for k, v in resp.headers.items()]
        payload = resp.json_bytes()
        if payload:
            headers.append(("Content-Type", resp.content_type))
        return request.make_response(payload, headers=headers, status=resp.status)
