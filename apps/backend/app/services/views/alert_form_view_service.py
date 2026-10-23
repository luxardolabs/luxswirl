"""
Alert form service - handles web form data parsing for alerts.

This service handles the parsing of web form data for alert creation/updates,
including checkbox collection and threshold extraction.
"""

from typing import Any
from uuid import UUID

from app.schemas.alert_schema import AlertFormData
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.alert_form")


class AlertFormViewService:
    """Service for parsing alert form data."""

    @staticmethod
    def parse_alert_form(form: dict[str, Any]) -> AlertFormData:
        """
        Parse alert form data for providers, checks, and thresholds.

        Extracts:
        - Provider IDs from checkboxes (prefix: "provider_")
        - Check IDs from checkboxes (prefix: "check_")
        - Days thresholds for SSL cert expiry (prefix: "trigger_days_threshold_")

        Args:
            form: Form data dictionary

        Returns:
            AlertFormData with parsed values
        """
        # Collect provider IDs from checkboxes (parsed to UUID at the boundary)
        provider_ids: list[UUID] = []
        for key in form:
            if key.startswith("provider_"):
                provider_ids.append(UUID(form[key]))

        # Collect check IDs from checkboxes (parsed to UUID at the boundary)
        check_ids: list[UUID] = []
        for key in form:
            if key.startswith("check_"):
                check_ids.append(UUID(form[key]))

        # Collect days thresholds for SSL cert expiry (if present)
        collected_thresholds: list[int] = []
        for key in form:
            if key.startswith("trigger_days_threshold_"):
                collected_thresholds.append(int(form[key]))

        # Sort thresholds for cleaner storage
        days_thresholds: list[int] | None
        if collected_thresholds:
            collected_thresholds.sort()
            days_thresholds = collected_thresholds
        else:
            days_thresholds = None  # Not an SSL cert expiry alert

        return AlertFormData(
            provider_ids=provider_ids,
            check_ids=check_ids,
            days_thresholds=days_thresholds,
        )
