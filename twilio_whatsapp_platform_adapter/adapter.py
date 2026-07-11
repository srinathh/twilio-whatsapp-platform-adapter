"""WhatsApp (Twilio Business API) platform adapter for Hermes Agent.

Registers a new ``twilio_whatsapp`` platform via the Hermes plugin registry —
distinct from the upstream ``whatsapp`` (Baileys personal-account bridge) and
``sms`` (Twilio SMS) platforms, but sharing the Twilio account credentials
with the latter.

Env vars:
  - TWILIO_ACCOUNT_SID              (shared with the SMS adapter)
  - TWILIO_AUTH_TOKEN               (shared with the SMS adapter)
  - TWILIO_WHATSAPP_NUMBER          (E.164 WhatsApp sender, e.g. +14155238886)
  - TWILIO_WHATSAPP_WEBHOOK_URL     (public URL Twilio calls — required for
                                     signature validation)
  - TWILIO_WHATSAPP_WEBHOOK_HOST    (default 0.0.0.0 — the tunnel connects in)
  - TWILIO_WHATSAPP_WEBHOOK_PORT    (default 8080)
  - TWILIO_WHATSAPP_ALLOWED_USERS   (comma-separated E.164 numbers)
  - TWILIO_WHATSAPP_ALLOW_ALL_USERS (true/false)
  - TWILIO_WHATSAPP_HOME_CHANNEL    (E.164 number for cron delivery)
  - TWILIO_WHATSAPP_INSECURE_NO_SIGNATURE (true to skip validation — dev only)
  - TWILIO_WHATSAPP_OUTBOUND_MEDIA_DIR    (override outbound media staging dir)
"""

import asyncio
import base64
import hmac
import logging
import mimetypes
import os
import shutil
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    get_document_cache_dir,
)
from gateway.platforms.helpers import redact_phone

logger = logging.getLogger(__name__)

from .helpers import (
    _ALLOWED_INBOUND_CONTENT_TYPES,
    _MEDIA_DOWNLOAD_MAX_BYTES,
    _MEDIA_MAX_PER_MESSAGE,
    _TWILIO_WEBHOOK_MAX_BODY_BYTES,
    DEFAULT_WEBHOOK_HOST,
    DEFAULT_WEBHOOK_PORT,
    MAX_WHATSAPP_LENGTH,
    TWILIO_API_BASE,
    WEBHOOK_PATH,
    WHATSAPP_PLATFORM_HINT,
    _extract_media_files,
    _from_wa,
    _normalize_whatsapp_formatting,
    _port_variant_url,
    _to_wa,
    compute_twilio_signature,
)


def check_requirements() -> bool:
    """Runtime deps + minimum Twilio WhatsApp credentials present."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(
        os.getenv("TWILIO_ACCOUNT_SID")
        and os.getenv("TWILIO_AUTH_TOKEN")
        and os.getenv("TWILIO_WHATSAPP_NUMBER")
    )


def _is_connected(config) -> bool:
    """Connected when the WhatsApp credentials are present (mirrors sms)."""
    return bool(
        (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
        and (os.getenv("TWILIO_WHATSAPP_NUMBER") or "").strip()
    )


def _env_enablement() -> Optional[dict]:
    """Auto-enable the platform when the required env vars are set."""
    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    number = (os.getenv("TWILIO_WHATSAPP_NUMBER") or "").strip()
    if not (sid and token and number):
        return None

    seed: Dict[str, Any] = {
        "from_number": number,
        "webhook_host": os.getenv("TWILIO_WHATSAPP_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST),
        "webhook_port": os.getenv("TWILIO_WHATSAPP_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT)),
    }
    home = (os.getenv("TWILIO_WHATSAPP_HOME_CHANNEL") or "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": home}
    return seed


class TwilioWhatsAppAdapter(BasePlatformAdapter):
    """Twilio WhatsApp <-> Hermes gateway adapter.

    Each inbound phone number gets its own Hermes session (multi-tenant).
    ``chat_id``/``user_id`` are stored as bare E.164 (no ``whatsapp:``
    prefix) so allowlists and cron delivery match the SMS convention; the
    prefix is applied at the Twilio API boundary only.
    """

    MAX_MESSAGE_LENGTH = MAX_WHATSAPP_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("twilio_whatsapp"))
        self._account_sid: str = os.environ["TWILIO_ACCOUNT_SID"]
        self._auth_token: str = os.environ["TWILIO_AUTH_TOKEN"]
        self._from_number: str = _from_wa(os.getenv("TWILIO_WHATSAPP_NUMBER", ""))
        self._webhook_port: int = int(
            os.getenv("TWILIO_WHATSAPP_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT))
        )
        self._webhook_host: str = os.getenv(
            "TWILIO_WHATSAPP_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST
        )
        self._webhook_url: str = os.getenv("TWILIO_WHATSAPP_WEBHOOK_URL", "").strip()
        self._outbound_dir: Path = Path(
            os.getenv("TWILIO_WHATSAPP_OUTBOUND_MEDIA_DIR", "").strip()
            or get_document_cache_dir().parent / "twilio_whatsapp_outbound"
        )
        self._runner = None
        self._http_session: Optional["aiohttp.ClientSession"] = None

    def _basic_auth_header(self) -> str:
        creds = f"{self._account_sid}:{self._auth_token}"
        encoded = base64.b64encode(creds.encode("ascii")).decode("ascii")
        return f"Basic {encoded}"

    # ------------------------------------------------------------------
    # Required abstract methods
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        import aiohttp
        from aiohttp import web

        if not self._from_number:
            msg = "[twilio_whatsapp] TWILIO_WHATSAPP_NUMBER not set — cannot send replies"
            logger.error(msg)
            self._set_fatal_error("twilio_whatsapp_missing_number", msg, retryable=False)
            return False

        insecure_no_sig = (
            os.getenv("TWILIO_WHATSAPP_INSECURE_NO_SIGNATURE", "").lower() == "true"
        )
        if not self._webhook_url and not insecure_no_sig:
            msg = (
                "[twilio_whatsapp] Refusing to start: TWILIO_WHATSAPP_WEBHOOK_URL is "
                "required for Twilio signature validation. Set it to the public URL "
                "configured in your Twilio console (e.g. "
                "https://example.com/webhooks/twilio-whatsapp). For local development "
                "without validation, set TWILIO_WHATSAPP_INSECURE_NO_SIGNATURE=true "
                "(NOT recommended for production)."
            )
            logger.error(msg)
            self._set_fatal_error(
                "twilio_whatsapp_missing_webhook_url", msg, retryable=False
            )
            return False

        if insecure_no_sig and not self._webhook_url:
            logger.warning(
                "[twilio_whatsapp] TWILIO_WHATSAPP_INSECURE_NO_SIGNATURE=true — "
                "signature validation is DISABLED. Any client that can reach port %d "
                "can inject messages. Do NOT use this in production.",
                self._webhook_port,
            )

        self._outbound_dir.mkdir(parents=True, exist_ok=True)

        app = web.Application(client_max_size=_TWILIO_WEBHOOK_MAX_BODY_BYTES)
        app.router.add_post(WEBHOOK_PATH, self._handle_webhook)
        app.router.add_get(WEBHOOK_PATH + "/media/{name}", self._serve_media)
        app.router.add_get(WEBHOOK_PATH + "/health", lambda _: web.Response(text="ok"))
        app.router.add_get("/health", lambda _: web.Response(text="ok"))

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._webhook_host, self._webhook_port)
        await site.start()
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        self._mark_connected()
        self._running = True

        logger.info(
            "[twilio_whatsapp] webhook server listening on %s:%d%s, from: %s",
            self._webhook_host,
            self._webhook_port,
            WEBHOOK_PATH,
            redact_phone(self._from_number),
        )
        return True

    async def disconnect(self) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        self._running = False
        logger.info("[twilio_whatsapp] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        import aiohttp

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

        local_media, remote_media = _extract_media_files(metadata)
        media_urls = list(remote_media)
        for path in local_media:
            staged = self._stage_media(path)
            if staged:
                media_urls.append(staged)
        media_urls = media_urls[:_MEDIA_MAX_PER_MESSAGE]

        last_result = SendResult(success=True)
        url = f"{TWILIO_API_BASE}/{self._account_sid}/Messages.json"
        headers = {"Authorization": self._basic_auth_header()}

        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        try:
            for index, chunk in enumerate(chunks):
                form_data = aiohttp.FormData()
                form_data.add_field("From", _to_wa(self._from_number))
                form_data.add_field("To", _to_wa(chat_id))
                form_data.add_field("Body", chunk)
                # Media rides on the first chunk only, so a split long reply
                # doesn't duplicate attachments.
                if index == 0:
                    for media_url in media_urls:
                        form_data.add_field("MediaUrl", media_url)

                try:
                    async with session.post(url, data=form_data, headers=headers) as resp:
                        body = await resp.json()
                        if resp.status >= 400:
                            error_msg = body.get("message", str(body))
                            logger.error(
                                "[twilio_whatsapp] send failed to %s: %s %s",
                                redact_phone(chat_id),
                                resp.status,
                                error_msg,
                            )
                            return SendResult(
                                success=False,
                                error=f"Twilio {resp.status}: {error_msg}",
                            )
                        msg_sid = body.get("sid", "")
                        last_result = SendResult(success=True, message_id=msg_sid)
                except Exception as e:
                    logger.error(
                        "[twilio_whatsapp] send error to %s: %s",
                        redact_phone(chat_id), e,
                    )
                    return SendResult(success=False, error=str(e))
        finally:
            if not self._http_session and session:
                await session.close()

        return last_result

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    # ------------------------------------------------------------------
    # WhatsApp formatting
    # ------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        """WhatsApp renders its own formatting — normalize, don't strip."""
        return _normalize_whatsapp_formatting(content or "")

    # ------------------------------------------------------------------
    # Twilio signature validation (identical to SMS — Twilio signs
    # WhatsApp webhooks the same way)
    # ------------------------------------------------------------------

    def _validate_twilio_signature(
        self, url: str, post_params: dict, signature: str,
    ) -> bool:
        if self._check_signature(url, post_params, signature):
            return True
        variant = _port_variant_url(url)
        if variant and self._check_signature(variant, post_params, signature):
            return True
        return False

    def _check_signature(
        self, url: str, post_params: dict, signature: str,
    ) -> bool:
        computed = compute_twilio_signature(self._auth_token, url, post_params)
        return hmac.compare_digest(computed, signature)

    # ------------------------------------------------------------------
    # Outbound media staging + serving
    # ------------------------------------------------------------------

    def _stage_media(self, local_path: str) -> Optional[str]:
        """Copy a local file into the outbound dir; return its public URL.

        Twilio fetches MediaUrl over the public webhook base, so the file
        must be reachable at {webhook_base}/media/<name>.
        """
        if not self._webhook_url:
            logger.warning(
                "[twilio_whatsapp] cannot stage outbound media without "
                "TWILIO_WHATSAPP_WEBHOOK_URL"
            )
            return None
        src = Path(local_path).resolve()
        if not src.is_file():
            logger.warning("[twilio_whatsapp] outbound media not found: %s", local_path)
            return None

        base = self._webhook_url.rstrip("/")
        if src.parent == self._outbound_dir.resolve():
            return f"{base}/media/{src.name}"

        filename = f"{uuid.uuid4().hex}{src.suffix}"
        dest = self._outbound_dir / filename
        try:
            self._outbound_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        except OSError as e:
            logger.error("[twilio_whatsapp] failed to stage media %s: %s", local_path, e)
            return None
        return f"{base}/media/{filename}"

    async def _serve_media(self, request) -> "aiohttp.web.StreamResponse":
        from aiohttp import web

        name = request.match_info.get("name", "")
        file_path = (self._outbound_dir / name).resolve()
        # Path-traversal guard: the resolved file must sit directly inside
        # the outbound dir.
        if file_path.parent != self._outbound_dir.resolve() or not file_path.is_file():
            return web.Response(status=404, text="Not found")
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        return web.FileResponse(path=file_path, headers={"Content-Type": content_type})

    # ------------------------------------------------------------------
    # Inbound media download
    # ------------------------------------------------------------------

    async def _download_inbound_media(
        self, items: Iterable[Tuple[str, str]],
    ) -> Tuple[List[str], List[str]]:
        """Download inbound Twilio media to Hermes' cache dirs.

        ``items`` is (url, content_type) pairs from the webhook form.
        Twilio media URLs need HTTP Basic auth; aiohttp drops the auth
        header on the cross-host redirect to storage, which is what Twilio
        expects. Returns (local paths, coarse media types).
        """
        import aiohttp

        paths: List[str] = []
        types: List[str] = []
        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        own_session = session is not self._http_session
        headers = {"Authorization": self._basic_auth_header()}
        try:
            for url, declared_type in items:
                try:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            logger.warning(
                                "[twilio_whatsapp] media download %s returned %d",
                                url[:120], resp.status,
                            )
                            continue
                        content_type = (
                            (resp.content_type or declared_type or "")
                            .split(";")[0].strip().lower()
                        )
                        if content_type and content_type not in _ALLOWED_INBOUND_CONTENT_TYPES:
                            logger.warning(
                                "[twilio_whatsapp] media %s disallowed content-type: %s",
                                url[:120], content_type,
                            )
                            continue
                        # aiohttp's StreamReader.read(n) short-reads (returns
                        # only the currently-buffered chunk), so accumulate the
                        # full body via iter_chunked, stopping once we exceed
                        # the cap.
                        buf = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            buf.extend(chunk)
                            if len(buf) > _MEDIA_DOWNLOAD_MAX_BYTES:
                                break
                        if len(buf) > _MEDIA_DOWNLOAD_MAX_BYTES:
                            logger.warning(
                                "[twilio_whatsapp] media %s exceeds %d bytes, skipping",
                                url[:120], _MEDIA_DOWNLOAD_MAX_BYTES,
                            )
                            continue
                        data = bytes(buf)
                        ext = mimetypes.guess_extension(content_type) or ".bin"
                        if content_type.startswith("image/"):
                            paths.append(cache_image_from_bytes(data, ext=ext))
                            types.append("image")
                        elif content_type.startswith("audio/"):
                            paths.append(cache_audio_from_bytes(data, ext=ext))
                            types.append("audio")
                        else:
                            paths.append(
                                cache_document_from_bytes(data, f"whatsapp_media{ext}")
                            )
                            types.append("document")
                except Exception as e:
                    logger.warning(
                        "[twilio_whatsapp] media download failed for %s: %s",
                        url[:120], e,
                    )
        finally:
            if own_session:
                await session.close()
        return paths, types

    # ------------------------------------------------------------------
    # Twilio webhook handler
    # ------------------------------------------------------------------

    async def _handle_webhook(self, request) -> "aiohttp.web.Response":
        from aiohttp import web

        def _twiml(status: int = 200) -> "web.Response":
            return web.Response(
                text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                content_type="application/xml",
                status=status,
            )

        try:
            content_length = request.content_length
            if content_length is not None and content_length > _TWILIO_WEBHOOK_MAX_BODY_BYTES:
                return _twiml(413)
            raw = await request.read()
            if len(raw) > _TWILIO_WEBHOOK_MAX_BODY_BYTES:
                return _twiml(413)
            # Twilio sends form-encoded data, not JSON
            form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except Exception as e:
            logger.error("[twilio_whatsapp] webhook parse error: %s", e)
            return _twiml(400)

        if self._webhook_url:
            twilio_sig = request.headers.get("X-Twilio-Signature", "")
            if not twilio_sig:
                logger.warning("[twilio_whatsapp] Rejected: missing X-Twilio-Signature header")
                return _twiml(403)
            flat_params = {k: v[0] for k, v in form.items() if v}
            if not self._validate_twilio_signature(
                self._webhook_url, flat_params, twilio_sig
            ):
                logger.warning("[twilio_whatsapp] Rejected: invalid Twilio signature")
                return _twiml(403)

        from_number = _from_wa((form.get("From", [""]))[0])
        to_number = _from_wa((form.get("To", [""]))[0])
        text = (form.get("Body", [""]))[0].strip()
        message_sid = (form.get("MessageSid", [""]))[0].strip()

        media_items: List[Tuple[str, str]] = []
        try:
            num_media = int((form.get("NumMedia", ["0"]))[0] or 0)
        except ValueError:
            num_media = 0
        for i in range(min(num_media, _MEDIA_MAX_PER_MESSAGE)):
            media_url = (form.get(f"MediaUrl{i}", [""]))[0].strip()
            media_type = (form.get(f"MediaContentType{i}", [""]))[0].strip()
            if media_url:
                media_items.append((media_url, media_type))

        if not from_number or (not text and not media_items):
            return _twiml()

        # Ignore messages from our own number (echo prevention)
        if from_number == self._from_number:
            logger.debug(
                "[twilio_whatsapp] ignoring echo from own number %s",
                redact_phone(from_number),
            )
            return _twiml()

        logger.info(
            "[twilio_whatsapp] inbound from %s -> %s (%d chars, %d media)",
            redact_phone(from_number),
            redact_phone(to_number),
            len(text),
            len(media_items),
        )

        local_media, media_types = await self._download_inbound_media(media_items)

        message_type = MessageType.TEXT
        if media_types:
            if media_types[0] == "image":
                message_type = MessageType.PHOTO
            elif media_types[0] == "audio":
                message_type = MessageType.AUDIO
            else:
                message_type = MessageType.DOCUMENT

        source = self.build_source(
            chat_id=from_number,
            chat_name=(form.get("ProfileName", [from_number]))[0] or from_number,
            chat_type="dm",
            user_id=from_number,
            user_name=(form.get("ProfileName", [from_number]))[0] or from_number,
            message_id=message_sid or None,
        )
        event = MessageEvent(
            text=text or "[WhatsApp media attachment]",
            message_type=message_type,
            source=source,
            raw_message=form,
            message_id=message_sid or None,
            media_urls=local_media,
            media_types=media_types,
        )

        # Non-blocking: Twilio expects a fast response
        task = asyncio.create_task(self._safe_handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # Empty TwiML — replies go via the REST API, not inline TwiML
        return _twiml()

    async def _safe_handle_message(self, event: MessageEvent) -> None:
        try:
            await self.handle_message(event)
        except Exception:
            logger.exception(
                "[twilio_whatsapp] unhandled error in handle_message for %s",
                event.message_id,
            )


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process WhatsApp delivery via the Twilio REST API (cron /
    home-channel). Implements the standalone_sender_fn contract."""
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    auth_token = getattr(pconfig, "api_key", None) or os.getenv("TWILIO_AUTH_TOKEN", "")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    from_number = _from_wa(os.getenv("TWILIO_WHATSAPP_NUMBER", ""))
    webhook_url = os.getenv("TWILIO_WHATSAPP_WEBHOOK_URL", "").strip()
    if not account_sid or not auth_token or not from_number:
        return {
            "error": "WhatsApp not configured (TWILIO_ACCOUNT_SID, "
                     "TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER required)"
        }

    message = _normalize_whatsapp_formatting(message or "")
    chunks = BasePlatformAdapter.truncate_message(message, MAX_WHATSAPP_LENGTH)

    # Stage local media into the outbound dir served by the gateway's
    # webhook server (shared filesystem: cron runs in the same container).
    media_urls: List[str] = []
    if media_files and webhook_url:
        outbound_dir = Path(
            os.getenv("TWILIO_WHATSAPP_OUTBOUND_MEDIA_DIR", "").strip()
            or get_document_cache_dir().parent / "twilio_whatsapp_outbound"
        )
        outbound_dir.mkdir(parents=True, exist_ok=True)
        base = webhook_url.rstrip("/")
        for f in media_files[:_MEDIA_MAX_PER_MESSAGE]:
            src = Path(str(f)).resolve()
            if not src.is_file():
                continue
            filename = f"{uuid.uuid4().hex}{src.suffix}"
            try:
                shutil.copyfile(src, outbound_dir / filename)
            except OSError:
                continue
            media_urls.append(f"{base}/media/{filename}")

    def _redacted_error(text):
        try:
            from tools.send_message_tool import _error as _e
            return _e(text)
        except Exception:
            return {"error": text}

    try:
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url()
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        creds = f"{account_sid}:{auth_token}"
        encoded = base64.b64encode(creds.encode("ascii")).decode("ascii")
        url = f"{TWILIO_API_BASE}/{account_sid}/Messages.json"
        headers = {"Authorization": f"Basic {encoded}"}
        message_id = ""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), **_sess_kw
        ) as session:
            for index, chunk in enumerate(chunks):
                form_data = aiohttp.FormData()
                form_data.add_field("From", _to_wa(from_number))
                form_data.add_field("To", _to_wa(chat_id))
                form_data.add_field("Body", chunk)
                if index == 0:
                    for media_url in media_urls:
                        form_data.add_field("MediaUrl", media_url)
                async with session.post(url, data=form_data, headers=headers, **_req_kw) as resp:
                    body = await resp.json()
                    if resp.status >= 400:
                        error_msg = body.get("message", str(body))
                        return _redacted_error(f"Twilio API error ({resp.status}): {error_msg}")
                    message_id = body.get("sid", "")
        return {
            "success": True,
            "platform": "twilio_whatsapp",
            "chat_id": chat_id,
            "message_id": message_id,
        }
    except Exception as e:
        return _redacted_error(f"WhatsApp send failed: {e}")


def _build_adapter(config):
    return TwilioWhatsAppAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="twilio_whatsapp",
        label="WhatsApp (Twilio)",
        adapter_factory=_build_adapter,
        check_fn=check_requirements,
        is_connected=_is_connected,
        required_env=[
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_WHATSAPP_NUMBER",
        ],
        install_hint="pip install aiohttp",
        env_enablement_fn=_env_enablement,
        allowed_users_env="TWILIO_WHATSAPP_ALLOWED_USERS",
        allow_all_env="TWILIO_WHATSAPP_ALLOW_ALL_USERS",
        cron_deliver_env_var="TWILIO_WHATSAPP_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_WHATSAPP_LENGTH,
        pii_safe=True,
        emoji="💬",
        allow_update_command=True,
        platform_hint=WHATSAPP_PLATFORM_HINT,
    )
