"""Gated live test — sends ONE real WhatsApp message via the Twilio REST API.

Run only with real credentials and an explicit opt-in:

    TWILIO_WHATSAPP_LIVE_TEST=1 TWILIO_WHATSAPP_LIVE_TO=+65XXXXXXXX \
        pytest tests/test_live.py -m live
"""

import asyncio
import os

import pytest

pytestmark = pytest.mark.live

_ENABLED = os.getenv("TWILIO_WHATSAPP_LIVE_TEST", "") == "1"


@pytest.mark.skipif(not _ENABLED, reason="TWILIO_WHATSAPP_LIVE_TEST != 1")
def test_live_send_returns_sid():
    pytest.importorskip("gateway", reason="requires a Hermes environment")
    from twilio_whatsapp_platform_adapter.adapter import _standalone_send

    to = os.environ["TWILIO_WHATSAPP_LIVE_TO"]
    result = asyncio.run(
        _standalone_send(
            None,
            to,
            "twilio-whatsapp-platform-adapter live test — please ignore",
        )
    )
    assert result.get("success") is True, result
    assert result.get("message_id", "").startswith("SM")
