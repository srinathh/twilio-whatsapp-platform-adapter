# twilio-whatsapp-platform-adapter

WhatsApp (Twilio Business API) platform adapter plugin for
[Hermes Agent](https://hermes-agent.nousresearch.com). Registers a new
`twilio_whatsapp` platform via the pip entry-point group `hermes_agent.plugins`.

## What this is (and isn't)

- A **pip entry-point plugin**: Hermes' `_scan_entry_points()` imports
  `twilio_whatsapp_platform_adapter.adapter` and calls `register(ctx)`.
  The entry-point *name* `twilio_whatsapp-platform` matters — Hermes strips the
  trailing `-platform` to derive the platform id, which must equal
  `register_platform(name="twilio_whatsapp")`.
- **Not** the upstream `whatsapp` platform (that's a Baileys personal-account
  bridge) and **not** the upstream `sms` platform (Twilio SMS) — but it shares
  `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` with the latter. Never set
  `TWILIO_PHONE_NUMBER` in a deployment using this plugin, or the SMS adapter
  may spuriously enable.
- `plugin.yaml` is optional metadata for the interactive `hermes config` UI
  only; runtime behavior comes entirely from `register()` kwargs.

## Architecture

- `twilio_whatsapp_platform_adapter/helpers.py` — pure functions ONLY (no
  `gateway.*` imports). Signature computation (hand-rolled HMAC-SHA1 per
  Twilio's docs — deliberately **no `twilio` package dependency**),
  `whatsapp:` addressing, Markdown→WhatsApp normalization, media extraction.
  Keep it importable outside Hermes so `tests/test_static.py` stays runnable
  anywhere.
- `twilio_whatsapp_platform_adapter/adapter.py` — the
  `TwilioWhatsAppAdapter(BasePlatformAdapter)` and `register(ctx)`. Imports
  `gateway.*`, so it only imports inside a Hermes environment. Modeled on the
  upstream bundled `plugins/platforms/sms/adapter.py` (aiohttp webhook +
  Twilio REST send) with the telnyx-adapter dynamic-platform pattern
  (`Platform("twilio_whatsapp")`).
- `chat_id`/`user_id` are **bare E.164** everywhere inside Hermes (allowlists,
  cron `--deliver twilio_whatsapp:+65...`); the `whatsapp:` prefix is applied
  only at the Twilio API boundary (`_to_wa`/`_from_wa`).
- Outbound media is staged into `TWILIO_WHATSAPP_OUTBOUND_MEDIA_DIR` (default:
  sibling of Hermes' document cache) and served back to Twilio at
  `{TWILIO_WHATSAPP_WEBHOOK_URL}/media/<uuid><ext>` by the same aiohttp app.
  Inbound media downloads use HTTP Basic auth (Twilio media URLs require it)
  with a 5 MB cap and a content-type allowlist, landing in Hermes' cache dirs.

## Testing (no mocks — house rule)

- `tests/test_static.py`: pure-helper tests, run anywhere:
  `uv run --with pytest --with pyyaml pytest tests/test_static.py`
- `tests/test_runtime.py`: needs the Hermes venv — run inside a deployed
  container: `docker exec hermes /opt/hermes/.venv/bin/python -m pytest <path>/tests`.
  Spins up the real aiohttp server and asserts unsigned POSTs get 403.
- `tests/test_live.py`: gated by `TWILIO_WHATSAPP_LIVE_TEST=1`; sends one real
  WhatsApp message. Never run in CI.

## Release / consumption

Consumed by the private `hermes_agent` deployment repo via a git-tag pin in
its `python-requirements.txt`:

```
twilio-whatsapp-platform-adapter @ git+https://github.com/srinathh/twilio-whatsapp-platform-adapter@v0.1.0
```

To ship a change: commit → `git tag v0.x.y` → push tag → bump the pin in
`hermes_agent/python-requirements.txt` → normal hermes_agent tag+deploy cycle
(see that repo's `docs/git-ops-lite.md`).

## Design spec

Full design rationale lives in the hermes_agent repo:
`docs/plans/look-at-the-upstream-pure-kurzweil.md`. Twilio specifics worth
remembering: 1600-char body limit (error 21617), max 10 `MediaUrl` per
message, webhook is form-encoded (not JSON), `X-Twilio-Signature` covers the
exact public URL + sorted POST params (validate against both the portless and
`:443` URL variants).
