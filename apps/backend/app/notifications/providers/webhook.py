"""
Webhook Notification Provider.

Sends notifications via HTTP POST to a specified webhook URL.
"""

from typing import Any

import httpx
from fastapi.encoders import jsonable_encoder

from app.core.check_target_validator import blocked_target_error
from app.core.credential_mask import mask_url_paths
from app.core.http_header import sendable_headers
from app.notifications.providers._vetted_http import vetted_transport
from app.notifications.providers.base import (
    BaseNotificationProvider,
    NotificationContext,
)
from app.shared.logger import get_logger
from app.shared.ssrf import SsrfBlockedError

logger = get_logger("luxswirl.notifications.webhook")


class WebhookNotificationProvider(BaseNotificationProvider):
    """
    Webhook notification provider.

    Sends HTTP POST requests to a configured webhook URL with check status data.
    Supports custom headers and JSON body presets.
    """

    provider_type = "webhook"

    def validate_config(self, config: dict[str, Any]) -> None:
        """
        Validate webhook provider configuration.

        Required fields:
        - post_url: Webhook URL to POST to

        Optional fields:
        - request_body_preset: "json" or "form" (default: "json")
        - additional_headers: Dict of extra headers to send, each name an
          HTTP token and each value printable ASCII (`sendable_headers`)
        - timeout: Request timeout in seconds (default: 10)
        - verify_ssl: Whether to verify SSL certificates (default: True)
        """
        required_fields = ["post_url"]
        for field in required_fields:
            if field not in config:
                raise ValueError(f"Webhook provider missing required field: {field}")

        # Validate URL format (basic check)
        url = config["post_url"]
        if not url.startswith(("http://", "https://")):
            raise ValueError(
                f"Invalid webhook URL: {url}. Must start with http:// or https://"
            )

        # SSRF protection is judged where the stored settings are: on save
        # (`network_targets`, off the event loop) and on every connection
        # (`vetted_transport`). Nothing here resolves the name.

        # Validate request body preset
        preset = config.get("request_body_preset", "json")
        if preset not in ["json", "form"]:
            raise ValueError(
                f"Invalid request_body_preset: {preset}. Must be 'json' or 'form'"
            )

        if "additional_headers" in config:
            sendable_headers("additional_headers", config["additional_headers"])

    def network_targets(self) -> list[str]:
        """The webhook URL, which the SSRF settings judge when it is saved."""
        return [self.config["post_url"]]

    async def send(self, context: NotificationContext) -> bool:
        """
        Send webhook notification.

        Args:
            context: Notification context

        Returns:
            True if webhook was sent successfully (2xx status code)

        Raises:
            CheckTargetBlockedError: The SSRF settings refuse every address of its host.
            Exception: If webhook fails to send
        """
        # Build request data
        url = self.config["post_url"]
        preset = self.config.get("request_body_preset", "json")
        timeout = self.config.get("timeout", 10)
        verify_ssl = self.config.get("verify_ssl", True)

        # Build payload (encode UUID/datetime to JSON-native types at the wire boundary)
        payload = jsonable_encoder(self._build_payload(context))

        # Build headers
        headers = self._build_headers(preset)

        # Judged again before the client opens: a header h11 refuses is
        # refused on an open connection, which it leaves unclosed.
        if "additional_headers" in self.config:
            headers.update(
                sendable_headers(
                    "additional_headers", self.config["additional_headers"]
                )
            )

        # Send request, on an address the SSRF settings allow (DNS rebinding included)
        transport = vetted_transport(verify=verify_ssl, network=self.network)
        try:
            async with httpx.AsyncClient(
                transport=transport, timeout=timeout
            ) as client:
                if preset == "json":
                    response = await client.post(url, json=payload, headers=headers)
                else:  # form
                    response = await client.post(url, data=payload, headers=headers)

                response.raise_for_status()
        except SsrfBlockedError as e:
            raise blocked_target_error(e) from e

        logger.info(
            "Webhook notification sent successfully",
            extra={"url": mask_url_paths(url), "status_code": response.status_code},
        )
        return True

    def _build_payload(self, context: NotificationContext) -> dict[str, Any]:
        """
        Build webhook payload from notification context.

        Args:
            context: Notification context

        Returns:
            Payload dictionary
        """
        return {
            "check_name": context.check_name,
            "check_type": context.check_type,
            "target": context.target,
            "agent_id": context.agent_id,
            "agent_name": context.agent_name,
            "status": context.status,
            "success": context.success,
            "previous_status": context.previous_status,
            "latency_ms": context.latency_ms,
            "timestamp": context.timestamp,
            "error_message": context.error_message,
            "error_type": context.error_type,
            "detail": context.detail,
            "http_status_code": context.http_status_code,
            "alert_name": context.alert_name,
            "alert_description": context.alert_description,
            "is_recovery": context.is_recovery,
            "consecutive_failures": context.consecutive_failures,
        }

    def _build_headers(self, preset: str) -> dict[str, str]:
        """
        Build HTTP headers based on preset.

        Args:
            preset: Request body preset type

        Returns:
            Headers dictionary
        """
        if preset == "json":
            return {
                "Content-Type": "application/json",
                "User-Agent": "LuxSwirl-Monitor/1.0",
            }
        else:  # form
            return {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "LuxSwirl-Monitor/1.0",
            }

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Get configuration schema for webhook provider."""
        return {
            "post_url": {
                "type": "string",
                "label": "Post URL",
                "required": True,
                "help_text": "Webhook URL to POST check status updates to",
                "placeholder": "https://example.com/api/webhook/your-webhook-id",
            },
            "request_body_preset": {
                "type": "select",
                "label": "Request Body",
                "required": False,
                "default": "json",
                "options": [
                    {"value": "json", "label": "Preset - application/json"},
                    {
                        "value": "form",
                        "label": "Form - application/x-www-form-urlencoded",
                    },
                ],
                "help_text": "application/json is good for any modern HTTP servers such as Express.js",
            },
            "additional_headers": {
                "type": "json",
                "label": "Additional Headers",
                "required": False,
                "help_text": "Sets additional headers sent with the webhook, as a JSON object of name/value. A value must be printable ASCII, with no line break or other control character.",
                "placeholder": '{"Authorization": "Bearer token123", "X-Custom": "value"}',
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
    def get_provider_description(cls) -> str:
        """Get provider description."""
        return "Send HTTP POST requests to webhook URLs for real-time alerting"
