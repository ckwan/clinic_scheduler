"""Twilio webhook authentication.

Twilio signs each request (X-Twilio-Signature, an HMAC of the exact public URL plus the form
fields, keyed by your auth token). Without checking it, anyone who finds the URL could post fake
patient messages. The check fails closed: if validation is on but not configured, requests are
refused instead of accepted.
"""

from fastapi import HTTPException, Request
from twilio.request_validator import RequestValidator

from app.config import settings


def public_url_for(request: Request) -> str:
    """The URL Twilio actually called. Behind ngrok or a proxy, request.url shows the internal
    address, so the configured public base URL is used instead."""
    url = settings.public_base_url.rstrip("/") + request.url.path
    return f"{url}?{request.url.query}" if request.url.query else url


async def verified_form(request: Request) -> dict[str, str]:
    """FastAPI dependency: the form fields of an authentic Twilio request."""
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    if not settings.twilio_validate_signature:
        return params

    if not settings.twilio_auth_token or not settings.public_base_url:
        raise HTTPException(
            status_code=503,
            detail="Signature validation is on but TWILIO_AUTH_TOKEN or PUBLIC_BASE_URL is not set",
        )
    signature = request.headers.get("X-Twilio-Signature", "")
    if not RequestValidator(settings.twilio_auth_token).validate(public_url_for(request), params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return params
