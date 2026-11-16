"""The check a bulk-create request asks for, once its URL is classified and named."""

from app.models.enum_model import CheckType
from app.schemas.check_schema import (
    DEFAULT_RETRY_INTERVAL_SECONDS,
    BulkCheckCreateRequest,
    CheckCreate,
)


def bulk_check_create(
    req: BulkCheckCreateRequest, check_type: str, url: str, display_name: str
) -> CheckCreate:
    """`req`'s settings as the check it makes: `check_type` and `url` from the
    URL's classification, `display_name` the unused name chosen for it."""
    return CheckCreate(
        display_name=display_name,
        check_type=CheckType(check_type),
        target=url,
        interval_seconds=req.interval_seconds,
        timeout_seconds=req.timeout_seconds or 10,
        enabled=req.enabled,
        tags=req.tags,
        expected_status=req.expected_status,
        http_method=req.http_method,
        verify_ssl=req.verify_ssl,
        resend_notification_after=req.resend_notification_after,
        retry_attempts=req.retry_attempts,
        # Unset takes CheckCreate's own default, as the single form does.
        retry_interval_seconds=(
            DEFAULT_RETRY_INTERVAL_SECONDS
            if req.retry_interval_seconds is None
            else req.retry_interval_seconds
        ),
    )
