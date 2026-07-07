# twilio-whatsapp-platform-adapter

WhatsApp channel for [Hermes Agent](https://hermes-agent.nousresearch.com) via
the **Twilio Business API**. Registers a distinct `twilio_whatsapp` platform —
independent of Hermes' built-in `whatsapp` (Baileys personal bridge) and `sms`
(Twilio SMS) platforms, while sharing Twilio account credentials with the
latter.

## Features

- Inbound via Twilio webhook (form-encoded POST, `X-Twilio-Signature`
  HMAC-SHA1 validation — hand-rolled, no `twilio` package).
- Outbound via the Twilio REST API with 1600-char chunking (error 21617).
- Media both ways: inbound downloads (Basic auth, 5 MB cap, content-type
  allowlist) into Hermes' media cache; outbound staged and served back to
  Twilio over the public webhook URL (max 10 per message).
- WhatsApp-native formatting via `platform_hint` + light Markdown
  normalization (`**bold**` → `*bold*`, headers → bold, links → bare URL).
- Per-number sessions, allowlist (`TWILIO_WHATSAPP_ALLOWED_USERS`), cron
  delivery (`--deliver twilio_whatsapp:+65...`), out-of-process standalone
  sender.

## Install

Pip entry-point route (recommended — Hermes discovers it on boot):

```
uv pip install "twilio-whatsapp-platform-adapter @ git+https://github.com/srinathh/twilio-whatsapp-platform-adapter@v0.1.0"
```

Or copy `twilio_whatsapp_platform_adapter/` + `plugin.yaml` into
`~/.hermes/plugins/twilio_whatsapp/`.

## Configuration

See [.env.example](.env.example). Required: `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER`, `TWILIO_WHATSAPP_WEBHOOK_URL`.

Point the Twilio console (Messaging → your WhatsApp sender → webhook) at
`https://<your-host>/webhooks/twilio-whatsapp`. The webhook server binds
`0.0.0.0:8080` by default — put it behind TLS ingress (e.g. a Cloudflare
Tunnel); never expose it directly.

**Caveat:** don't set `TWILIO_PHONE_NUMBER` alongside this plugin, or Hermes'
built-in SMS adapter may also enable on the shared credentials.

## Tests

```
uv run --with pytest --with pyyaml pytest tests/test_static.py   # anywhere
docker exec hermes /opt/hermes/.venv/bin/python -m pytest <path>/tests  # in Hermes
TWILIO_WHATSAPP_LIVE_TEST=1 TWILIO_WHATSAPP_LIVE_TO=+65... pytest -m live  # one real msg
```
