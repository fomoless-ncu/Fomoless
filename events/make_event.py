"""Generate a signed LINE webhook event for `sam local invoke`.

Usage:
    python events/make_event.py "本週活動" > events/text_message.json
    sam local invoke LineWebhookFunction -e events/text_message.json

Reads the channel secret from env.json (LineWebhookFunction.LINE_CHANNEL_SECRET).
Note: replying needs a real reply token, so the local run will log a LINE API error
after the signature check and DynamoDB query succeed. That is expected.
"""

import base64
import hashlib
import hmac
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
secret = json.loads((ROOT / "env.json").read_text(encoding="utf-8"))["LineWebhookFunction"]["LINE_CHANNEL_SECRET"]
text = sys.argv[1] if len(sys.argv) > 1 else "本週活動"

body = json.dumps(
    {
        "destination": "Udeadbeefdeadbeefdeadbeefdeadbeef",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "timestamp": int(time.time() * 1000),
                "webhookEventId": "01LOCALTEST0000000000000000",
                "deliveryContext": {"isRedelivery": False},
                "replyToken": "00000000000000000000000000000000",
                "source": {"type": "user", "userId": "U00000000000000000000000000000000"},
                "message": {"id": "1", "type": "text", "quoteToken": "q", "text": text},
            }
        ],
    },
    ensure_ascii=False,
)
signature = base64.b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).decode()

print(json.dumps(
    {
        "version": "2.0",
        "routeKey": "POST /webhook",
        "rawPath": "/webhook",
        "headers": {"content-type": "application/json", "x-line-signature": signature},
        "body": body,
        "isBase64Encoded": False,
    },
    ensure_ascii=False,
    indent=2,
))
