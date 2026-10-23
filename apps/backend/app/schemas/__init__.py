"""
Schemas package - Pydantic models for API validation and serialization.
"""

from app.schemas.agent_schema import (
    AgentCreate,
    AgentResponse,
    AgentUpdate,
)
from app.schemas.alert_schema import (
    AlertCheckMappingCreate,
    AlertCheckMappingUpdate,
    AlertCreate,
    AlertNotificationMappingCreate,
    AlertNotificationMappingUpdate,
    AlertResponse,
    AlertStatsResponse,
    AlertUpdate,
)
from app.schemas.auth_schema import (
    ChangePasswordRequest,
    CurrentUserResponse,
    SessionResponse,
)
from app.schemas.base_schema import (
    BaseSchema,
    ErrorResponse,
    ResponseSchema,
    TimestampSchema,
)
from app.schemas.check_result_schema import (
    AgentReportRequest,
    AgentReportResponse,
    CheckResultCreate,
)
from app.schemas.check_schema import (
    CheckCreate,
    CheckListResponse,
    CheckResponse,
    CheckUpdate,
)
from app.schemas.notification_provider_schema import (
    NotificationProviderCreate,
    NotificationProviderUpdate,
)
from app.schemas.user_schema import (
    UserCreate,
    UserResponse,
    UserStatsResponse,
    UserUpdate,
)

__all__ = [
    # Base
    "BaseSchema",
    "TimestampSchema",
    "ResponseSchema",
    "ErrorResponse",
    # Auth
    "ChangePasswordRequest",
    "SessionResponse",
    "CurrentUserResponse",
    # User
    "UserCreate",
    "UserUpdate",
    "UserResponse",
    "UserStatsResponse",
    # Agent
    "AgentCreate",
    "AgentUpdate",
    "AgentResponse",
    # Check
    "CheckCreate",
    "CheckUpdate",
    "CheckResponse",
    "CheckListResponse",
    # CheckResult
    "CheckResultCreate",
    "AgentReportRequest",
    "AgentReportResponse",
    # NotificationProvider
    "NotificationProviderCreate",
    "NotificationProviderUpdate",
    # Alert
    "AlertCreate",
    "AlertUpdate",
    "AlertResponse",
    "AlertNotificationMappingCreate",
    "AlertNotificationMappingUpdate",
    "AlertCheckMappingCreate",
    "AlertCheckMappingUpdate",
    "AlertStatsResponse",
]
