"""
Base Notification Provider - Abstract class for all notification providers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID


@dataclass
class NotificationContext:
    """
    Context data for notifications.

    Contains all the information needed to render and send a notification.
    """

    # Check information (required)
    check_name: str
    check_type: str
    target: str
    agent_id: UUID | None
    status: str  # "success", "error", "no_data" (a silent check), "offline" (an agent)

    # Optional fields
    agent_name: str | None = None
    success: bool = False
    previous_status: str | None = None
    latency_ms: float | None = None
    timestamp: str | None = None
    error_message: str | None = None
    error_type: str | None = None
    http_status_code: int | None = None
    alert_name: str | None = None
    alert_description: str | None = None
    is_recovery: bool = False
    consecutive_failures: int = 0
    custom_subject: str | None = None
    custom_message: str | None = None
    # What the alert found, said in a line, for a trigger whose news is not the
    # check's status (an SSL certificate's expiry): shown even on a success.
    detail: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def status_label(self) -> str:
        """The status a person reads: UP, DOWN, NO DATA or OFFLINE."""
        if self.success:
            return "UP"
        return {"no_data": "NO DATA", "offline": "OFFLINE"}.get(self.status, "DOWN")


class BaseNotificationProvider(ABC):
    """
    Abstract base class for all notification providers.

    All notification providers (Email, Webhook, HomeAssistant, etc.)
    must inherit from this class and implement the required methods.
    """

    # Provider type identifier (must be unique)
    provider_type: ClassVar[str] = ""

    def __init__(self, config: dict[str, Any]):
        """
        Initialize the notification provider with configuration.

        Args:
            config: Provider-specific configuration dictionary

        Raises:
            ValueError: If configuration is invalid
        """
        self.validate_config(config)
        self.config = config

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> None:
        """
        Validate the provider configuration.

        Args:
            config: Configuration dictionary to validate

        Raises:
            ValueError: If configuration is invalid
        """

    @abstractmethod
    async def send(self, context: NotificationContext) -> bool:
        """
        Send a notification using this provider.

        Args:
            context: Notification context containing all relevant data

        Returns:
            True if notification was sent successfully, False otherwise

        Raises:
            Exception: If notification fails to send (will be logged)
        """

    @classmethod
    @abstractmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """
        Get the configuration schema for this provider.

        Returns a schema describing all configuration fields needed for this
        provider. Used for dynamic form generation in the UI.

        Returns:
            Dictionary describing configuration fields

        Example:
            {
                "hostname": {
                    "type": "string",
                    "label": "SMTP Hostname",
                    "required": True,
                    "help_text": "SMTP server hostname"
                },
                "port": {
                    "type": "integer",
                    "label": "Port",
                    "default": 587,
                    "required": True
                }
            }
        """

    @classmethod
    def get_provider_name(cls) -> str:
        """
        Get human-readable name of this provider.

        Returns:
            Provider display name
        """
        return cls.provider_type.replace("_", " ").title()

    @classmethod
    def get_provider_description(cls) -> str:
        """
        Get description of this provider.

        Returns:
            Provider description
        """
        return f"{cls.get_provider_name()} notification provider"

    def format_subject(
        self,
        context: NotificationContext,
        template: str | None = None,
    ) -> str:
        """
        Format notification subject line with variable substitution.

        Args:
            context: Notification context
            template: Custom subject template (uses default if None)

        Returns:
            Formatted subject line
        """
        if not template:
            template = self.get_default_subject_template()

        return self._substitute_variables(template, context)

    def format_message(
        self,
        context: NotificationContext,
        template: str | None = None,
    ) -> str:
        """
        Format notification message body with variable substitution.

        Args:
            context: Notification context
            template: Custom message template (uses default if None)

        Returns:
            Formatted message body
        """
        if not template:
            template = self.get_default_message_template()

        return self._substitute_variables(template, context)

    def _substitute_variables(self, template: str, context: NotificationContext) -> str:
        """
        Substitute template variables with actual values.

        Supports the following variables:
        - {{NAME}} - Check name
        - {{HOSTNAME_OR_URL}} - Target
        - {{STATUS}} - Status (UP, DOWN, NO DATA, OFFLINE)
        - {{LATENCY}} - Response time
        - {{AGENT}} - Agent name/ID
        - {{TIMESTAMP}} - When check ran
        - {{ERROR_MESSAGE}} - Error details
        - {{DETAIL}} - What the alert found (an SSL certificate's expiry)
        - {{TYPE}} - Check type
        - {{ALERT}} - Alert name

        Args:
            template: Template string with {{VARIABLES}}
            context: Notification context

        Returns:
            Template with variables substituted
        """
        replacements = {
            "{{NAME}}": context.check_name,
            "{{HOSTNAME_OR_URL}}": context.target,
            "{{STATUS}}": context.status_label,
            "{{LATENCY}}": (
                f"{context.latency_ms:.2f}ms" if context.latency_ms else "N/A"
            ),
            "{{AGENT}}": context.agent_name or "Unknown Agent",
            "{{TIMESTAMP}}": context.timestamp or "N/A",
            "{{ERROR_MESSAGE}}": context.error_message or "No error",
            "{{DETAIL}}": context.detail or "N/A",
            "{{TYPE}}": context.check_type,
            "{{ALERT}}": context.alert_name or "Unknown Alert",
            "{{HTTP_STATUS}}": (
                str(context.http_status_code) if context.http_status_code else "N/A"
            ),
        }

        result = template
        for variable, value in replacements.items():
            result = result.replace(variable, str(value))

        return result

    @classmethod
    def get_default_subject_template(cls) -> str:
        """
        Get default subject line template.

        Returns:
            Default subject template
        """
        return "[{{STATUS}}] {{NAME}} - {{HOSTNAME_OR_URL}}"

    @classmethod
    def get_default_message_template(cls) -> str:
        """
        Get default message body template.

        Returns:
            Default message template
        """
        return """
Check: {{NAME}}
Type: {{TYPE}}
Target: {{HOSTNAME_OR_URL}}
Status: {{STATUS}}
Latency: {{LATENCY}}
Agent: {{AGENT}}
Timestamp: {{TIMESTAMP}}
Error: {{ERROR_MESSAGE}}
Detail: {{DETAIL}}
""".strip()

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return f"<{self.__class__.__name__}(type={self.provider_type!r})>"
