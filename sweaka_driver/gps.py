"""POST /gps/batches → sweaka.driver.location points (T9.6.7). Any invalid point → whole batch 400."""
from datetime import timezone

from .lib.sweaka_connect_core import Problem
from .lib.sweaka_connect_core.feed import iso
from .sales import _when


class GpsService:
    def __init__(self, wms):
        self.wms, self.env = wms, wms.env

    def create(self, driver, body):
        user = self.wms._user(driver)
        points = body.get("points")
        if not isinstance(points, list) or not 1 <= len(points) <= 500:
            raise Problem.field("points")
        rows = []
        for i, p in enumerate(points):
            if not isinstance(p, dict):
                raise Problem.field(f"points[{i}]")
            loc = p.get("location")
            if not isinstance(loc, dict) or set(loc) != {"lat", "lon"}:
                raise Problem.field(f"points[{i}].location")
            for axis, bound in (("lat", 90), ("lon", 180)):
                v = loc[axis]
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not -bound <= v <= bound:
                    raise Problem.field(f"points[{i}].location.{axis}")
            at = _when(p.get("captured_at"), f"points[{i}].captured_at")
            for f in ("accuracy_m", "speed_kmh"):
                if f not in p:
                    raise Problem.field(f"points[{i}].{f}", "required")
                v = p[f]
                if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                    raise Problem.field(f"points[{i}].{f}")
            rows.append({"driver_id": user.id, "latitude": loc["lat"], "longitude": loc["lon"],
                         "accuracy_m": p["accuracy_m"] or 0, "speed_kmh": p["speed_kmh"] or 0, "captured_at": at})
        times = sorted(r["captured_at"] for r in rows)
        batch = self.env["sweaka.driver.gps.batch"].sudo().create({
            "client_id": body["client_id"], "driver_id": user.id, "points_count": len(rows),
            "first_captured_at": times[0], "last_captured_at": times[-1]})
        self.env["sweaka.driver.location"].sudo().create([dict(r, batch_id=batch.id) for r in rows])
        return self.batch_data(batch)

    @staticmethod
    def batch_data(batch):
        to_utc = lambda dt: iso(dt.replace(tzinfo=timezone.utc)) if dt else None  # noqa: E731
        return {"id": str(batch.id), "client_id": batch.client_id, "points_count": batch.points_count,
                "first_captured_at": to_utc(batch.first_captured_at), "last_captured_at": to_utc(batch.last_captured_at),
                "created_at": to_utc(batch.create_date)}
