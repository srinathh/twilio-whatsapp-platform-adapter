"""WhatsApp (Twilio) platform plugin entry point for Hermes Agent."""

try:
    from .adapter import register
except ImportError:  # pragma: no cover — helpers stay importable outside Hermes
    register = None

__all__ = ["register"]
