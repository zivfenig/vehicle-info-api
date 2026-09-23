"""
Vehicle Info API - a thin wrapper around Encore's vehicle-info endpoint.

WHY THIS SERVICE EXISTS
  Encore answers in a different shape for every outcome:
      200  {"success": true, "data": {...}}
      404  {"detail": {"success": false, "error": "...Hebrew..."}}
      422  {"detail": [{"msg": "...Hebrew...", "loc": [...], ...}]}
  and their docs promise 400 for a bad format while they actually return 422.
  A conversation flow cannot branch on that.

  This service turns every outcome into ONE shape carrying a stable
  error_code, so the flow branches on a fixed value and writes its own
  wording - in Hebrew or English, its choice.

THE CONTRACT - if this code answers, it answers 200
  {"success": true,  "data": {...}}
  {"success": false, "error_code": "<CODE>", "reason": "...", "message": "..."}

  error_code             meaning                                  customer can...
  VEHICLE_NOT_FOUND      plate is valid but not in the registry   fix the plate
  INVALID_PLATE          registry rejected the format (+ reason)  fix the plate
  UPSTREAM_BUSY          registry overloaded (429 / 503)          retry shortly
  UPSTREAM_TIMEOUT       registry did not answer in time          retry shortly
  UPSTREAM_UNAVAILABLE   registry unreachable                     retry later
  UPSTREAM_ERROR         registry failed in any other way         retry later
  INVALID_REQUEST        the caller sent a malformed body         (config bug)

WHY EVERYTHING IS 200
  The Insait platform treats any non-2xx as a tool failure, and a failure
  either extracts nothing (if the body is not our JSON) or is replaced by the
  tool's Fallback Response. Either way our error_code would be lost.

  So the rule is: if OUR code produced the answer, it is 200 and carries an
  error_code. Anything non-2xx therefore did NOT come from our code - Cloud
  Run is down, the URL is wrong, the network failed - and the tool's Fallback
  Response turns it into SERVICE_UNAVAILABLE. Every lookup always updates the
  flow's variables, and a 404 can never be mistaken for "vehicle not found".

  Upstream failures are logged as warnings, so monitoring does not depend on
  the HTTP status.

LANGUAGE
  `message` is English, for logs only. `reason` is Encore's own explanation of
  a rejected format, passed through so the flow can tell the customer the rule
  without us copying it (a copy drifts). The flow owns every word the customer
  sees and translates as needed.
"""

import logging
import os
import re
from typing import Any, Dict, Optional, Union

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

UPSTREAM_URL = os.getenv(
    "UPSTREAM_URL",
    "https://insurance-webhook-945894769129.us-central1.run.app/vehicle-info",
)
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "10"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vehicle-info")

app = FastAPI(
    title="Vehicle Info API",
    description="Wraps Encore's vehicle-info endpoint for the Insait flow.",
    version="1.1.0",
)


class VehicleRequest(BaseModel):
    # str or int: a chat platform may send the plate as a number.
    license_plate: Union[str, int]


def ok(data: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=200, content={"success": True, "data": data})


def fail(
    error_code: str,
    message: str,
    license_plate: Optional[str] = None,
    reason: Optional[str] = None,
) -> JSONResponse:
    body: Dict[str, Any] = {
        "success": False,
        "error_code": error_code,
        "message": message,
    }
    if reason:
        body["reason"] = reason
    if license_plate is not None:
        body["license_plate"] = license_plate
    return JSONResponse(status_code=200, content=body)


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return None


def is_real_not_found(r: httpx.Response) -> bool:
    """Encore's genuine not-found body: {"detail": {"success": false, ...}}.
    A wrong route returns {"detail": "Not Found"} - that is not a missing car."""
    body = _json(r)
    detail = body.get("detail") if isinstance(body, dict) else None
    return isinstance(detail, dict) and detail.get("success") is False


def format_reason(r: httpx.Response) -> Optional[str]:
    """Encore's own explanation of why the format was rejected, if present.
    422: {"detail": [{"msg": "Value error, <text>"}]}
    400: {"success": false, "error": "<text>"}   (their documented shape)"""
    body = _json(r)
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        msg = str(detail[0].get("msg", ""))
        return re.sub(r"^Value error,\s*", "", msg).strip() or None
    if isinstance(body.get("error"), str):
        return body["error"].strip() or None
    return None


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness probe, and a handy keep-warm target before a demo."""
    return {"status": "ok"}


@app.post("/vehicle-info")
async def vehicle_info(payload: VehicleRequest) -> JSONResponse:
    # Tidy the input; do NOT judge its format - that is Encore's rule.
    plate = re.sub(r"[\s\-.]", "", str(payload.license_plate))

    if not plate:
        return fail("INVALID_PLATE", "Empty license plate.")

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            r = await client.post(UPSTREAM_URL, json={"license_plate": plate})
    except httpx.TimeoutException:
        # Must be caught before RequestError - TimeoutException subclasses it.
        log.warning("Upstream timed out for %s", plate)
        return fail("UPSTREAM_TIMEOUT", "Encore did not respond in time.", plate)
    except httpx.RequestError as exc:
        log.warning("Upstream unreachable: %s", exc)
        return fail("UPSTREAM_UNAVAILABLE", f"Cannot reach Encore: {exc}", plate)

    # Their docs say 400 for a bad format; in practice they return 422.
    if r.status_code in (400, 422):
        return fail("INVALID_PLATE", "Encore rejected the plate format.", plate,
                    reason=format_reason(r))

    if r.status_code == 404:
        if is_real_not_found(r):
            return fail("VEHICLE_NOT_FOUND", "No vehicle for this plate.", plate)
        log.warning("Upstream route not found (404) for %s", plate)
        return fail("UPSTREAM_ERROR", "Encore route not found.", plate)

    if r.status_code in (429, 503):
        log.warning("Upstream busy (%s) for %s", r.status_code, plate)
        return fail("UPSTREAM_BUSY", f"Encore is busy ({r.status_code}).", plate)

    if r.status_code != 200:
        log.warning("Upstream %s for %s", r.status_code, plate)
        return fail("UPSTREAM_ERROR", f"Encore returned {r.status_code}.", plate)

    body = _json(r)
    if not isinstance(body, dict):  # e.g. an HTML page instead of JSON
        log.warning("Upstream returned non-JSON for %s", plate)
        return fail("UPSTREAM_ERROR", "Encore returned non-JSON.", plate)

    data = body.get("data")
    if not body.get("success") or not isinstance(data, dict) or not data:
        # 200 without vehicle data is not a documented outcome.
        log.warning("Upstream 200 without data for %s", plate)
        return fail("UPSTREAM_ERROR", "Encore returned 200 without data.", plate)

    plate_out = data.get("license_plate", plate)
    year = data.get("year")
    manufacturer = data.get("manufacturer")
    model = data.get("model")
    color = data.get("color")

    # One ready-to-speak string with no label and no language of its own.
    car = " ".join(str(x) for x in (year, manufacturer, model) if x)
    display = ", ".join(str(x) for x in (plate_out, car, color) if x)

    return ok(
        {
            "license_plate": plate_out,
            "manufacturer": manufacturer,
            "model": model,
            "year": year,
            "color": color,
            "display": display,
        }
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Malformed request body - a caller bug, not a bad plate."""
    return fail("INVALID_REQUEST", "Body must contain 'license_plate'.")