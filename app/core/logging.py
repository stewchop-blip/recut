"""Structured logging configuration with secret masking."""

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from app.core.config import get_settings


def mask_secrets_processor(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Mask sensitive values in log entries."""
    settings = get_settings()
    sensitive_keys = {
        "telegram_bot_token",
        "openrouter_api_key",
        "webhook_secret",
        "database_url",
        "api_key",
        "token",
        "secret",
        "password",
        "authorization",
    }

    def mask_value(key: str, value: Any) -> Any:
        key_lower = key.lower()
        if any(sensitive in key_lower for sensitive in sensitive_keys):
            if isinstance(value, str) and value:
                return f"***{value[-4:]}" if len(value) > 4 else "****"
            return "****"
        return value

    def walk_dict(d: dict) -> dict:
        return {k: mask_value(k, walk_dict(v) if isinstance(v, dict) else v) for k, v in d.items()}

    if isinstance(event_dict.get("event"), str):
        # Mask in log message itself
        msg = event_dict["event"]
        for secret in [settings.telegram_bot_token, settings.openrouter_api_key, settings.webhook_secret]:
            if secret and secret in msg:
                msg = msg.replace(secret, f"***{secret[-4:]}")
        event_dict["event"] = msg

    # Mask in key-value pairs
    for key, value in list(event_dict.items()):
        if isinstance(value, dict):
            event_dict[key] = walk_dict(value)
        else:
            event_dict[key] = mask_value(key, value)

    return event_dict


def add_job_context(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add job_id and user_id to log context if available in contextvars."""
    # ContextVars would be set by middleware
    return event_dict


def setup_logging() -> None:
    """Configure structlog for the application."""
    settings = get_settings()

    # Standard library logging config
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level),
    )

    # Shared processors
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        mask_secrets_processor,
        add_job_context,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure formatter for stdlib handlers
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True) if not settings.is_production
        else structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(getattr(logging, settings.log_level))

    # Reduce noise from libraries
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)