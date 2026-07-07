"""Static tests — no Hermes runtime, no network, no mocks.

Exercise the pure helper functions (signature, addressing, formatting,
media extraction) directly. The signature test recomputes Twilio's
documented HMAC-SHA1 algorithm independently and compares.
"""

import base64
import hashlib
import hmac
from pathlib import Path

import yaml

from twilio_whatsapp_platform_adapter.helpers import (
    MAX_WHATSAPP_LENGTH,
    WEBHOOK_PATH,
    _extract_media_files,
    _from_wa,
    _normalize_whatsapp_formatting,
    _port_variant_url,
    _to_wa,
    compute_twilio_signature,
)


def test_to_wa_prefixes_bare_e164():
    assert _to_wa("+6598204137") == "whatsapp:+6598204137"
    assert _to_wa("whatsapp:+6598204137") == "whatsapp:+6598204137"
    assert _to_wa(" +14155238886 ") == "whatsapp:+14155238886"


def test_from_wa_strips_prefix():
    assert _from_wa("whatsapp:+6598204137") == "+6598204137"
    assert _from_wa("+6598204137") == "+6598204137"
    assert _from_wa("") == ""


def test_signature_matches_twilio_documented_algorithm():
    # Twilio's documented example algorithm, computed independently here:
    # https://www.twilio.com/docs/usage/security#validating-requests
    auth_token = "12345678901234567890123456789012"
    url = "https://hermes.example.com/webhooks/twilio-whatsapp"
    params = {
        "From": "whatsapp:+6598204137",
        "To": "whatsapp:+14155238886",
        "Body": "Hello, world!",
        "MessageSid": "SM00000000000000000000000000000000",
        "NumMedia": "0",
    }
    expected_base = url + "".join(k + params[k] for k in sorted(params))
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), expected_base.encode(), hashlib.sha1).digest()
    ).decode()

    assert compute_twilio_signature(auth_token, url, params) == expected
    # A tampered param must not validate
    tampered = dict(params, Body="Hello, world")
    assert compute_twilio_signature(auth_token, url, tampered) != expected


def test_port_variant_url():
    assert (
        _port_variant_url("https://hermes.example.com/webhooks/twilio-whatsapp")
        == "https://hermes.example.com:443/webhooks/twilio-whatsapp"
    )
    assert (
        _port_variant_url("https://hermes.example.com:443/webhooks/twilio-whatsapp")
        == "https://hermes.example.com/webhooks/twilio-whatsapp"
    )
    assert _port_variant_url("https://hermes.example.com:8443/x") is None


def test_normalize_whatsapp_formatting():
    assert _normalize_whatsapp_formatting("**bold**") == "*bold*"
    assert _normalize_whatsapp_formatting("# Heading\nbody") == "*Heading*\nbody"
    assert (
        _normalize_whatsapp_formatting("[site](https://example.com)")
        == "site (https://example.com)"
    )
    # WhatsApp-native formatting passes through untouched
    assert _normalize_whatsapp_formatting("*b* _i_ ~s~ ```m```") == "*b* _i_ ~s~ ```m```"


def test_extract_media_files_splits_local_and_remote():
    local, remote = _extract_media_files(
        {
            "media_files": ["/opt/data/cache/images/img_1.jpg"],
            "media_urls": ["https://example.com/a.png"],
            "attachments": [{"url": "https://example.com/b.pdf"}, {"path": "/tmp/c.ogg"}],
        }
    )
    assert local == ["/opt/data/cache/images/img_1.jpg", "/tmp/c.ogg"]
    assert remote == ["https://example.com/a.png", "https://example.com/b.pdf"]
    assert _extract_media_files(None) == ([], [])
    assert _extract_media_files({}) == ([], [])


def test_constants():
    assert MAX_WHATSAPP_LENGTH == 1600
    assert WEBHOOK_PATH == "/webhooks/twilio-whatsapp"


def test_plugin_yaml_parses_and_matches():
    manifest = yaml.safe_load(
        (Path(__file__).parent.parent / "plugin.yaml").read_text()
    )
    assert manifest["name"] == "twilio-whatsapp-platform"
    assert manifest["kind"] == "platform"
    required = {e["name"] for e in manifest["requires_env"]}
    assert {"TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_NUMBER"} <= required
