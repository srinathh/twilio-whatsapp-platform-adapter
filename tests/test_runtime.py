"""Runtime tests — require the Hermes environment (run inside the container):

    docker exec hermes /opt/hermes/.venv/bin/python -m pytest <repo>/tests

Skipped automatically where ``gateway`` is not importable.
"""

import importlib.util
import os

import pytest

pytest.importorskip("gateway", reason="requires a Hermes environment")

os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
os.environ.setdefault("TWILIO_AUTH_TOKEN", "0" * 32)
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER", "+14155238886")
os.environ.setdefault(
    "TWILIO_WHATSAPP_WEBHOOK_URL",
    "https://hermes.example.com/webhooks/twilio-whatsapp",
)

# The dynamic Platform("twilio_whatsapp") pseudo-member only exists once the
# plugin registry has registered the platform — same as a real gateway boot.
from hermes_cli.plugins import discover_plugins  # noqa: E402

discover_plugins()

from twilio_whatsapp_platform_adapter import adapter as adapter_mod  # noqa: E402


def test_entry_point_registered():
    from importlib.metadata import entry_points

    eps = entry_points(group="hermes_agent.plugins")
    names = {ep.name for ep in eps}
    if "twilio_whatsapp-platform" not in names:
        pytest.skip("package not pip-installed (running from checkout)")
    ep = next(ep for ep in eps if ep.name == "twilio_whatsapp-platform")
    assert ep.value == "twilio_whatsapp_platform_adapter.adapter"


def test_adapter_constructs_with_dynamic_platform():
    from gateway.config import PlatformConfig

    config = PlatformConfig(enabled=True)
    a = adapter_mod.TwilioWhatsAppAdapter(config)
    assert a.MAX_MESSAGE_LENGTH == 1600
    assert a._from_number == "+14155238886"


def test_check_requirements_true_with_env():
    assert adapter_mod.check_requirements() is True


async def test_webhook_rejects_unsigned_post():
    """Real aiohttp server, real HTTP request, no mocks: an unsigned POST
    must get a 403 when a webhook URL is configured."""
    import aiohttp
    from gateway.config import PlatformConfig

    os.environ["TWILIO_WHATSAPP_WEBHOOK_PORT"] = "18099"
    os.environ["TWILIO_WHATSAPP_WEBHOOK_HOST"] = "127.0.0.1"
    config = PlatformConfig(enabled=True)
    a = adapter_mod.TwilioWhatsAppAdapter(config)
    assert await a.connect() is True
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://127.0.0.1:18099/webhooks/twilio-whatsapp",
                data={"From": "whatsapp:+6500000000", "Body": "hi"},
            ) as resp:
                assert resp.status == 403
            async with session.get(
                "http://127.0.0.1:18099/webhooks/twilio-whatsapp/health"
            ) as resp:
                assert resp.status == 200
                assert (await resp.text()) == "ok"
            # Path traversal on the media route must 404, not leak files
            async with session.get(
                "http://127.0.0.1:18099/webhooks/twilio-whatsapp/media/..%2F..%2Fetc%2Fpasswd"
            ) as resp:
                assert resp.status == 404
    finally:
        await a.disconnect()
