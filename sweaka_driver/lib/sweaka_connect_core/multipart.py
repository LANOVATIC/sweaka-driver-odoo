"""Minimal multipart/form-data parser (stdlib email) for POST /uploads (T9.1.4h.1). → {name: (content_type, bytes)}."""
from email.parser import BytesParser
from email.policy import HTTP


def parse(content_type, raw):
    if not content_type.lower().startswith("multipart/form-data"):
        return {}
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw)
    out = {}
    if not msg.is_multipart():
        return out
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            out[name] = (part.get_content_type(), part.get_payload(decode=True) or b"")
    return out
