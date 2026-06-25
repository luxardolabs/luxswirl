"""
Email (SMTP) Notification Provider.
"""

import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import aiosmtplib
from shared.logger import get_logger

from app.notifications.providers.base import BaseNotificationProvider, NotificationContext


class EmailNotificationProvider(BaseNotificationProvider):
    """
    Email notification provider using SMTP.

    Supports various SMTP configurations including Gmail, Office365, and custom SMTP servers.
    """

    provider_type = "email"

    @property
    def logger(self):
        """Get logger instance (lazy initialization to ensure proper configuration)."""
        return get_logger("luxswirl.notifications.email")

    def validate_config(self, config: dict[str, Any]) -> None:
        """
        Validate email provider configuration.

        Required fields:
        - hostname: SMTP server hostname
        - port: SMTP server port
        - from_email: Sender email address
        - to_email: Recipient email address(es)

        Optional fields:
        - username: SMTP authentication username
        - password: SMTP authentication password
        - security: "none", "starttls", or "ssl"
        - cc: CC recipients
        - bcc: BCC recipients
        - ignore_tls_error: Ignore TLS certificate errors
        - custom_subject: Custom subject template
        """
        required_fields = ["hostname", "port", "from_email", "to_email"]
        for field in required_fields:
            if field not in config:
                raise ValueError(f"Email provider missing required field: {field}")

        # Validate port
        try:
            port = int(config["port"])
            if port < 1 or port > 65535:
                raise ValueError("Port must be between 1 and 65535")
        except ValueError, TypeError:
            raise ValueError(f"Invalid port: {config.get('port')}") from None

        # Validate email addresses (basic validation)
        for email_field in ["from_email", "to_email"]:
            email = config.get(email_field, "")
            if not email or "@" not in email:
                raise ValueError(f"Invalid email address in {email_field}: {email}")

        # Validate security type
        security = config.get("security", "none")
        if security not in ["none", "starttls", "ssl"]:
            raise ValueError(
                f"Invalid security type: {security}. Must be 'none', 'starttls', or 'ssl'"
            )

    async def send(self, context: NotificationContext) -> bool:
        """
        Send email notification.

        Args:
            context: Notification context

        Returns:
            True if email was sent successfully

        Raises:
            Exception: If email fails to send
        """
        try:
            self.logger.debug(
                "Starting email send",
                extra={"to_email": self.config["to_email"]},
            )
            self.logger.debug(
                "SMTP config",
                extra={
                    "smtp_hostname": self.config["hostname"],
                    "smtp_port": self.config["port"],
                    "security": self.config.get("security", "none"),
                },
            )

            # Build email message
            msg = self._build_message(context)
            self.logger.debug(
                "Email message built",
                extra={"subject": msg["Subject"]},
            )

            # Send via async SMTP
            self.logger.debug("Initiating SMTP connection...")
            await self._send_smtp(msg)

            self.logger.info(
                "Email notification sent successfully",
                extra={"to_email": self.config["to_email"]},
            )
            return True

        except Exception:
            self.logger.error("Failed to send email notification", exc_info=True)
            raise

    def _build_message(self, context: NotificationContext) -> MIMEMultipart:
        """
        Build MIME email message.

        Args:
            context: Notification context

        Returns:
            MIME multipart message
        """
        # Create message
        msg = MIMEMultipart("alternative")

        # Set headers
        msg["From"] = self.config["from_email"]
        msg["To"] = self.config["to_email"]

        # Optional CC/BCC
        if self.config.get("cc"):
            msg["Cc"] = self.config["cc"]
        if self.config.get("bcc"):
            msg["Bcc"] = self.config["bcc"]

        # Subject
        subject_template = (
            context.custom_subject
            or self.config.get("custom_subject")
            or self.get_default_subject_template()
        )
        msg["Subject"] = self.format_subject(context, subject_template)

        # Body - plain text
        text_template = (
            context.custom_message
            or self.config.get("custom_message")
            or self.get_default_message_template()
        )
        text_body = self.format_message(context, text_template)

        # Body - HTML (enhanced version)
        html_body = self._build_html_body(context)

        # Attach both plain text and HTML
        part_text = MIMEText(text_body, "plain")
        part_html = MIMEText(html_body, "html")

        msg.attach(part_text)
        msg.attach(part_html)

        return msg

    def _build_html_body(self, context: NotificationContext) -> str:
        """
        Build HTML email body with nice formatting.

        Args:
            context: Notification context

        Returns:
            HTML email body
        """
        # Determine status color
        if context.success:
            status_color = "#10b981"  # green
            status_text = "✓ UP"
        else:
            status_color = "#ef4444"  # red
            status_text = "✗ DOWN"

        # Build HTML
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: {status_color}; color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9fafb; padding: 20px; border: 1px solid #e5e7eb; border-radius: 0 0 8px 8px; }}
        .field {{ margin-bottom: 12px; }}
        .label {{ font-weight: 600; color: #374151; }}
        .value {{ color: #6b7280; }}
        .footer {{ margin-top: 20px; padding-top: 20px; border-top: 1px solid #e5e7eb; font-size: 12px; color: #9ca3af; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">{status_text} {context.check_name}</h2>
        </div>
        <div class="content">
            <div class="field">
                <span class="label">Check Name:</span>
                <span class="value">{context.check_name}</span>
            </div>
            <div class="field">
                <span class="label">Type:</span>
                <span class="value">{context.check_type}</span>
            </div>
            <div class="field">
                <span class="label">Target:</span>
                <span class="value">{context.target}</span>
            </div>
            <div class="field">
                <span class="label">Status:</span>
                <span class="value" style="color: {status_color}; font-weight: 600;">{status_text}</span>
            </div>
            <div class="field">
                <span class="label">Response Time:</span>
                <span class="value">{f"{context.latency_ms:.2f}ms" if context.latency_ms is not None else "N/A"}</span>
            </div>
            <div class="field">
                <span class="label">Agent:</span>
                <span class="value">{context.agent_name or "Unknown Agent"}</span>
            </div>
            <div class="field">
                <span class="label">Timestamp:</span>
                <span class="value">{context.timestamp}</span>
            </div>
"""

        # Add error message if present
        if not context.success and context.error_message:
            html += f"""
            <div class="field">
                <span class="label">Error:</span>
                <span class="value" style="color: #dc2626;">{context.error_message}</span>
            </div>
"""

        # Add HTTP status if present
        if context.http_status_code:
            html += f"""
            <div class="field">
                <span class="label">HTTP Status:</span>
                <span class="value">{context.http_status_code}</span>
            </div>
"""

        html += """
        </div>
        <div class="footer">
            Sent by LuxSwirl Monitoring System
        </div>
    </div>
</body>
</html>
""".strip()

        return html

    async def _send_smtp(self, msg: MIMEMultipart) -> None:
        """
        Send email via async SMTP.

        Args:
            msg: MIME message to send

        Raises:
            Exception: If SMTP send fails
        """
        hostname = self.config["hostname"]
        port = int(self.config["port"])
        security = self.config.get("security", "none")
        ignore_tls = self.config.get("ignore_tls_error", False)
        username = self.config.get("username")
        password = self.config.get("password")
        timeout = self.config.get("timeout_seconds", 30)

        self.logger.debug(
            "SMTP connection params",
            extra={
                "smtp_hostname": hostname,
                "smtp_port": port,
                "security": security,
                "timeout_seconds": timeout,
            },
        )
        self.logger.debug(
            "SMTP authentication",
            extra={
                "username_set": bool(username),
                "password_set": bool(password),
            },
        )

        # Configure TLS context
        tls_context = None
        if security in ("ssl", "starttls"):
            tls_context = ssl.create_default_context()
            if ignore_tls:
                self.logger.debug("TLS certificate verification disabled")
                tls_context.check_hostname = False
                tls_context.verify_mode = ssl.CERT_NONE
            else:
                self.logger.debug("TLS certificate verification enabled")

        # Determine connection parameters
        use_tls = security == "ssl"
        start_tls = security == "starttls"

        self.logger.debug(
            "Connection mode",
            extra={"use_tls": use_tls, "start_tls": start_tls},
        )

        # Send email using aiosmtplib
        try:
            self.logger.debug(
                "Attempting SMTP connection",
                extra={"smtp_hostname": hostname, "smtp_port": port},
            )
            await aiosmtplib.send(
                msg,
                hostname=hostname,
                port=port,
                username=username,
                password=password,
                use_tls=use_tls,
                start_tls=start_tls,
                tls_context=tls_context,
                timeout=timeout,
            )
            self.logger.debug("SMTP send completed successfully")
        except Exception:
            self.logger.error("SMTP send failed", exc_info=True)
            raise

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Get configuration schema for email provider."""
        return {
            "hostname": {
                "type": "string",
                "label": "Hostname",
                "required": True,
                "placeholder": "smtp.gmail.com",
                "help_text": "Either enter the hostname of the server you want to connect to or localhost if you intend to use a locally configured mail transfer agent",
            },
            "port": {
                "type": "integer",
                "label": "Port",
                "required": True,
                "default": 587,
                "placeholder": "587",
            },
            "security": {
                "type": "select",
                "label": "Security",
                "required": True,
                "default": "starttls",
                "options": [
                    {"value": "none", "label": "None (25)"},
                    {"value": "starttls", "label": "STARTTLS (587)"},
                    {"value": "ssl", "label": "SSL/TLS (465)"},
                ],
            },
            "ignore_tls_error": {
                "type": "boolean",
                "label": "Ignore TLS Error",
                "default": False,
            },
            "username": {
                "type": "string",
                "label": "Username",
                "required": False,
                "placeholder": "user@example.com",
            },
            "password": {
                "type": "password",
                "label": "Password",
                "required": False,
            },
            "from_email": {
                "type": "email",
                "label": "From Email",
                "required": True,
                "placeholder": "notifications@example.com",
            },
            "to_email": {
                "type": "email",
                "label": "To Email",
                "required": True,
                "placeholder": "admin@example.com",
            },
            "cc": {
                "type": "email",
                "label": "CC",
                "required": False,
            },
            "bcc": {
                "type": "email",
                "label": "BCC",
                "required": False,
            },
            "custom_subject": {
                "type": "string",
                "label": "Custom Subject",
                "required": False,
                "placeholder": "[{{STATUS}}] {{NAME}} - {{HOSTNAME_OR_URL}}",
                "help_text": "Leave blank for default. Variables: {{NAME}}, {{HOSTNAME_OR_URL}}, {{STATUS}}, {{LATENCY}}, {{AGENT}}, {{TIMESTAMP}}",
            },
            "timeout_seconds": {
                "type": "integer",
                "label": "Timeout (seconds)",
                "required": False,
                "default": 30,
                "placeholder": "30",
                "help_text": "SMTP connection timeout in seconds",
            },
        }

    @classmethod
    def get_provider_description(cls) -> str:
        """Get provider description."""
        return "Send notifications via email using SMTP. Supports Gmail, Office365, and custom SMTP servers."
