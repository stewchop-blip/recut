"""Structured logging configuration with secret masking."""

import logging
import sys
from urllib.parse import unquote, urlsplit
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

    import os
    secrets = [settings.telegram_bot_token, settings.openrouter_api_key,
               settings.webhook_secret, settings.database_url,
               os.getenv("TIKTOK_COOKIES_B64", ""), os.getenv("INSTAGRAM_COOKIES_B64", ""),
               os.getenv("INSTAGRAM_RESOLVER_SECRET", "")]
    sensitive_keys.update({"cookie", "credential"})
    proxy = os.getenv("INSTAGRAM_PROXY_URL", "").strip()
    secrets.append(proxy)
    sensitive_keys.update({"proxy_url", "proxy_auth"})
    try:
        parsed_proxy = urlsplit(proxy)
        for component in (parsed_proxy.username, parsed_proxy.password):
            if component:
                secrets.extend((component, unquote(component)))
    except ValueError:
        pass

    def redact(value: Any, key: str = "") -> Any:
        if any(s in key.lower() for s in sensitive_keys):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {k: redact(v, str(k)) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [redact(v) for v in value]
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
        return value

    return redact(event_dict)


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
