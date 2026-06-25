"""
Home Assistant Notification Provider.

Sends notifications to Home Assistant via webhook.
"""

from typing import Any

import httpx
from fastapi.encoders import jsonable_encoder
from shared.logger import get_logger

from app.notifications.providers.base import BaseNotificationProvider, NotificationContext

logger = get_logger("luxswirl.notifications.homeassistant")


class HomeAssistantNotificationProvider(BaseNotificationProvider):
    """
    Home Assistant notification provider.

    Sends webhook notifications to Home Assistant for integration with
    automations, dashboards, and other Home Assistant features.
    """

    provider_type = "homeassistant"

    def validate_config(self, config: dict[str, Any]) -> None:
        """
        Validate Home Assistant provider configuration.

        Required fields:
        - post_url: Home Assistant webhook URL

        Optional fields:
        - timeout: Request timeout in seconds (default: 10)
        - verify_ssl: Whether to verify SSL certificates (default: True)
        """
        required_fields = ["post_url"]
        for field in required_fields:
            if field not in config:
                raise ValueError(f"Home Assistant provider missing required field: {field}")

        # Validate URL format
        url = config["post_url"]
        if not url.startswith(("http://", "https://")):
            raise ValueError(
                f"Invalid Home Assistant URL: {url}. Must start with http:// or https://"
            )

        # Validate it looks like a Home Assistant webhook
        if "/api/webhook/" not in url:
            logger.warning(
                "URL doesn't appear to be a Home Assistant webhook endpoint "
                "(expected format: https://your-ha.com/api/webhook/webhook-id)",
                extra={"url": url},
            )

    async def send(self, context: NotificationContext) -> bool:
        """
        Send notification to Home Assistant.

        Args:
            context: Notification context

        Returns:
            True if notification was sent successfully

        Raises:
            Exception: If notification fails to send
        """
        try:
            # Build request
            url = self.config["post_url"]
            timeout = self.config.get("timeout", 10)
            verify_ssl = self.config.get("verify_ssl", True)

            # Build Home Assistant-friendly payload (encode UUID/datetime at the wire boundary)
            payload = jsonable_encoder(self._build_ha_payload(context))

            # Send to Home Assistant
            async with httpx.AsyncClient(verify=verify_ssl, timeout=timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "LuxSwirl-Monitor/1.0",
                    },
                )
                response.raise_for_status()

            logger.info(
                "Home Assistant notification sent successfully",
                extra={"url": url, "status_code": response.status_code},
            )
            return True

        except Exception:
            logger.error("Failed to send Home Assistant notification", exc_info=True)
            raise

    def _build_ha_payload(self, context: NotificationContext) -> dict[str, Any]:
        """
        Build Home Assistant-optimized payload.

        Structures data in a way that's easy to use in HA automations.

        Args:
            context: Notification context

        Returns:
            Payload dictionary optimized for Home Assistant
        """
        # Main event data
        payload = {
            "event_type": "luxswirl_monitor_alert",
            "data": {
                # Check information
                "check": {
                    "name": context.check_name,
                    "type": context.check_type,
                    "target": context.target,
                },
                # Agent information
                "agent": {
                    "id": context.agent_id,
                    "name": context.agent_name,
                },
                # Status
                "status": {
                    "current": context.status,
                    "previous": context.previous_status,
                    "success": context.success,
                    "is_recovery": context.is_recovery,
                },
                # Performance
                "performance": {
                    "latency_ms": context.latency_ms,
                    "http_status_code": context.http_status_code,
                },
                # Error details (if any)
                "error": (
                    {
                        "message": context.error_message,
                        "type": context.error_type,
                    }
                    if context.error_message
                    else None
                ),
                # Alert information
                "alert": {
                    "name": context.alert_name,
                    "description": context.alert_description,
                    "consecutive_failures": context.consecutive_failures,
                },
                # Metadata
                "timestamp": context.timestamp,
            },
        }

        return payload

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Get configuration schema for Home Assistant provider."""
        return {
            "post_url": {
                "type": "string",
                "label": "Post URL",
                "required": True,
                "help_text": "Home Assistant webhook URL (e.g., https://your-ha.com/api/webhook/webhook-id)",
                "placeholder": "https://home.example.com/api/webhook/your-webhook-id",
            },
            "timeout": {
                "type": "integer",
                "label": "Timeout (seconds)",
                "required": False,
                "default": 10,
                "help_text": "Request timeout in seconds",
            },
            "verify_ssl": {
                "type": "boolean",
                "label": "Verify SSL Certificate",
                "required": False,
                "default": True,
                "help_text": "Verify SSL/TLS certificates (disable for self-signed certs)",
            },
        }

    @classmethod
    def get_provider_name(cls) -> str:
        """Get provider display name."""
        return "Home Assistant"

    @classmethod
    def get_provider_description(cls) -> str:
        """Get provider description."""
        return "Send webhook notifications to Home Assistant for automation and alerting"
