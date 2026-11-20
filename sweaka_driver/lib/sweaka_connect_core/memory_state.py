"""In-memory StatePort (Bridge dev/tests). Production adapters persist tokens hashed + idempotency rows."""
import hashlib
import secrets
import threading


class MemoryState:
    def __init__(self):
        self._lock = threading.Lock()
        self.tokens = {}  # sha256(token) → (driver_id, device_id)
        self.idem = {}  # (driver_id, key) → [fingerprint, status, body]

    def issue_token(self, driver, device):
        raw = secrets.token_urlsafe(32)
        with self._lock:
            for h, (d, dev) in list(self.tokens.items()):  # re-login same device revokes its old token
                if d == driver.id and dev == device["device_id"]:
                    del self.tokens[h]
            self.tokens[hashlib.sha256(raw.encode()).hexdigest()] = (driver.id, device["device_id"])
        return raw

    def token_driver(self, token):
        entry = self.tokens.get(hashlib.sha256(token.encode()).hexdigest())
        return entry[0] if entry else None

    def revoke_token(self, token):
        self.tokens.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def idem_get(self, driver_id, key):
        entry = self.idem.get((driver_id, key))
        return tuple(entry) if entry else None

    def idem_begin(self, driver_id, key, fingerprint):
        with self._lock:
            if (driver_id, key) in self.idem:
                return False
            self.idem[(driver_id, key)] = [fingerprint, None, None]
            return True

    def idem_finish(self, driver_id, key, status, body):
        if status is None:
            self.idem.pop((driver_id, key), None)
        else:
            self.idem[(driver_id, key)][1:] = [status, body]
