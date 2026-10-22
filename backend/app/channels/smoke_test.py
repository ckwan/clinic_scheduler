"""Send one real text to check your Twilio setup: `make twilio-check TO=+15551234567`.

Checks the credentials, sends a message, then polls for its delivery status. Use a number
verified on your Twilio trial account. Needs SMS_MOCK=false and the TWILIO_* settings.
"""

import argparse
import sys
import time

from app.config import settings

TERMINAL = {"delivered", "undelivered", "failed", "read"}


def missing_settings() -> list[str]:
    names = {
        "TWILIO_ACCOUNT_SID": settings.twilio_account_sid,
        "TWILIO_AUTH_TOKEN": settings.twilio_auth_token,
        "TWILIO_FROM_NUMBER": settings.twilio_from_number,
    }
    return [name for name, value in names.items() if not value]


def run(to: str, client=None, wait_seconds: int = 20, sleep=time.sleep) -> int:
    missing = missing_settings()
    if missing:
        print("Missing settings:", ", ".join(missing), "(set them in .env)")
        return 2
    if settings.sms_mock:
        print("SMS_MOCK=true, so nothing would be sent. Set SMS_MOCK=false in .env first.")
        return 2

    if client is None:
        from twilio.rest import Client

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    try:
        message = client.messages.create(
            to=to, from_=settings.twilio_from_number, body="Clinic Scheduler test message. No reply needed."
        )
    except Exception as exc:  # Twilio errors carry a code and a helpful message
        print(f"Send failed: {exc}")
        print("Common causes: wrong credentials, a recipient number that is not verified on a trial account, or a from number that is not yours.")
        return 1
    print(f"Sent. Message id {message.sid}, status {message.status}")

    status, fetched = message.status, message
    for _ in range(wait_seconds):
        if status in TERMINAL:
            break
        sleep(1)
        fetched = client.messages(message.sid).fetch()
        status = fetched.status
    print(f"Final status: {status}")
    if status in {"undelivered", "failed"}:
        print(f"Error code: {getattr(fetched, 'error_code', None)} (look it up at twilio.com/docs/api/errors)")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("to", help="your verified phone number in +1XXXXXXXXXX format")
    sys.exit(run(parser.parse_args().to))


if __name__ == "__main__":
    main()
