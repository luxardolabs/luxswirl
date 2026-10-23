"""Registers the built-in notification providers for every test.

Production code does this in `main.py`'s lifespan; tests skip the FastAPI
startup, so this plugin (in tests/conftest.py's `pytest_plugins`) registers
them instead: any test that sends a notification needs them. Idempotent — `NotificationRegistry.register` raises if a
type is re-registered, so we guard.
"""

from __future__ import annotations

import pytest
from app.notifications.providers.email import EmailNotificationProvider
from app.notifications.providers.homeassistant import HomeAssistantNotificationProvider
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.notifications.registry import NotificationRegistry


@pytest.fixture(autouse=True, scope="session")
def _register_notification_providers():
    """Ensure the built-in providers are registered before any test runs.

    Mirrors the production registration block in main.py's lifespan startup.
    """
    for ptype, cls in (
        ("email", EmailNotificationProvider),
        ("webhook", WebhookNotificationProvider),
        ("homeassistant", HomeAssistantNotificationProvider),
    ):
        if not NotificationRegistry.is_registered(ptype):
            NotificationRegistry.register(ptype, cls)
    yield
