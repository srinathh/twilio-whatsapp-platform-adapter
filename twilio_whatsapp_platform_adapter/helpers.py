"""Pure helpers for the Twilio WhatsApp adapter.

No gateway/hermes imports here — this module must stay importable outside a
Hermes environment so the static test suite can exercise the signature and
formatting logic directly.
"""

import base64
import hashlib
import hmac
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"
MAX_WHATSAPP_LENGTH = 1600  # Twilio error 21617 beyond this
DEFAULT_WEBHOOK_PORT = 8080
DEFAULT_WEBHOOK_HOST = "0.0.0.0"  # the Cloudflare Tunnel connects east-west
WEBHOOK_PATH = "/webhooks/twilio-whatsapp"
_TWILIO_WEBHOOK_MAX_BODY_BYTES = 65_536  # 64 KiB — Twilio payloads are small
_MEDIA_DOWNLOAD_MAX_BYTES = 16 * 1024 * 1024  # Twilio WhatsApp media limit
_MEDIA_MAX_PER_MESSAGE = 10  # Twilio caps MediaUrl at 10 per message

_ALLOWED_INBOUND_CONTENT_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "video/mp4", "video/3gpp",
    "audio/mpeg", "audio/ogg", "audio/amr", "audio/mp4",
    "application/pdf",
    "text/vcard",
})

WHATSAPP_PLATFORM_HINT = (
    "You are chatting via WhatsApp (Twilio Business API). WhatsApp uses its "
    "own formatting, not Markdown: *bold*, _italic_, ~strikethrough~, "
    "```monospace```. Use • or - for bullets. Do NOT use Markdown headers "
    "(#), **double-asterisk bold**, tables, or [links](url) — write bare "
    "URLs. Keep replies concise; messages over 1600 characters are split."
)


def _to_wa(number: str) -> str:
    """Prefix an E.164 number with ``whatsapp:`` if not already present."""
    number = (number or "").strip()
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


def _from_wa(wa_id: str) -> str:
    """Strip a ``whatsapp:`` prefix, returning the bare E.164 number."""
    return (wa_id or "").strip().removeprefix("whatsapp:")


def compute_twilio_signature(auth_token: str, url: str, params: Dict[str, str]) -> str:
    """Compute Twilio's request signature (base64 HMAC-SHA1).

    Algorithm: https://www.twilio.com/docs/usage/security#validating-requests
    — the full URL concatenated with each POST param name+value in sorted
    key order, HMAC-SHA1 with the auth token, base64-encoded.
    """
    data_to_sign = url
    for key in sorted(params.keys()):
        data_to_sign += key + params[key]
    mac = hmac.new(
        auth_token.encode("utf-8"),
        data_to_sign.encode("utf-8"),
        hashlib.sha1,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


def _port_variant_url(url: str) -> Optional[str]:
    """Return the URL with the default port toggled, or None.

    Twilio may sign with either the explicit-default-port or portless form
    of the same URL. Non-standard ports are never modified.
    """
    parsed = urllib.parse.urlparse(url)
    default_ports = {"https": 443, "http": 80}
    default_port = default_ports.get(parsed.scheme)
    if default_port is None:
        return None

    if parsed.port == default_port:
        return urllib.parse.urlunparse(
            (parsed.scheme, parsed.hostname, parsed.path,
             parsed.params, parsed.query, parsed.fragment)
        )
    elif parsed.port is None:
        netloc = f"{parsed.hostname}:{default_port}"
        return urllib.parse.urlunparse(
            (parsed.scheme, netloc, parsed.path,
             parsed.params, parsed.query, parsed.fragment)
        )
    return None


def _normalize_whatsapp_formatting(content: str) -> str:
    """Light Markdown→WhatsApp normalization.

    The platform_hint asks the model for WhatsApp formatting directly; this
    only catches the most common slips (``**bold**`` and ``# headers``)
    without the SMS adapter's hard strip.
    """
    content = re.sub(r"\*\*(.+?)\*\*", r"*\1*", content, flags=re.DOTALL)
    content = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", content, flags=re.MULTILINE)
    content = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1 (\2)", content)
    return content


def _extract_media_files(metadata: Optional[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Split media references from Hermes metadata into (local paths, URLs).

    Accepts the common shapes used across adapters: ``media_files``,
    ``media_urls``, ``media``, ``attachments`` — strings, dicts with
    ``url``/``path``, or lists thereof.
    """
    if not metadata:
        return [], []

    raw: List[Any] = []
    for key in ("media_files", "media_urls", "media", "attachments"):
        value = metadata.get(key)
        if value is None:
            continue
        raw.extend(value if isinstance(value, list) else [value])

    local: List[str] = []
    remote: List[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("url") or item.get("path") or item.get("media_url") or ""
        text = str(item).strip()
        if not text:
            continue
        if text.startswith(("http://", "https://")):
            remote.append(text)
        else:
            local.append(text)
    return local, remote


