"""
Schemas package - Pydantic models for API validation and serialization.
"""

from app.schemas.agent_schema import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    AgentStatsResponse,
    AgentUpdate,
)
from app.schemas.alert_schema import (
    AlertCheckMappingCreate,
    AlertCheckMappingUpdate,
    AlertCreate,
    AlertListResponse,
    AlertNotificationMappingCreate,
    AlertNotificationMappingUpdate,
    AlertResponse,
    AlertStatsResponse,
    AlertUpdate,
)
from app.schemas.auth_schema import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    SessionListResponse,
    SessionResponse,
)
from app.schemas.auth_schema import (
    UserResponse as AuthUserResponse,
)
from app.schemas.base import (
    BaseSchema,
    ErrorResponse,
    PaginatedResponse,
    PaginationParams,
    ResponseSchema,
    TimestampSchema,
)
from app.schemas.check_result_schema import (
    AgentReportRequest,
    AgentReportResponse,
    CheckHistoryResponse,
    CheckResultCreate,
    CheckResultListResponse,
    CheckResultResponse,
    CheckSummary,
)
from app.schemas.check_schema import (
    CheckCreate,
    CheckListResponse,
    CheckResponse,
    CheckUpdate,
)
from app.schemas.notification_provider_schema import (
    NotificationProviderCreate,
    NotificationProviderListResponse,
    NotificationProviderResponse,
    NotificationProviderSchemaResponse,
    NotificationProviderTestRequest,
    NotificationProviderTestResponse,
    NotificationProviderTypesResponse,
    NotificationProviderUpdate,
)
from app.schemas.user_schema import (
    UserCreate,
    UserListResponse,
    UserPasswordReset,
    UserResponse,
    UserStatsResponse,
    UserUpdate,
)

__all__ = [
    # Base
    "BaseSchema",
    "TimestampSchema",
    "ResponseSchema",
    "PaginationParams",
    "PaginatedResponse",
    "ErrorResponse",
    # Auth
    "LoginRequest",
    "LoginResponse",
    "ChangePasswordRequest",
    "SessionResponse",
    "SessionListResponse",
    "AuthUserResponse",
    # User
    "UserCreate",
    "UserUpdate",
    "UserPasswordReset",
    "UserResponse",
    "UserListResponse",
    "UserStatsResponse",
    # Agent
    "AgentCreate",
    "AgentUpdate",
    "AgentResponse",
    "AgentListResponse",
    "AgentStatsResponse",
    # Check
    "CheckCreate",
    "CheckUpdate",
    "CheckResponse",
    "CheckListResponse",
    # CheckResult
    "CheckResultCreate",
    "CheckResultResponse",
    "CheckResultListResponse",
    "CheckHistoryResponse",
    "CheckSummary",
    "AgentReportRequest",
    "AgentReportResponse",
    # NotificationProvider
    "NotificationProviderCreate",
    "NotificationProviderUpdate",
    "NotificationProviderResponse",
    "NotificationProviderListResponse",
    "NotificationProviderTestRequest",
    "NotificationProviderTestResponse",
    "NotificationProviderSchemaResponse",
    "NotificationProviderTypesResponse",
    # Alert
    "AlertCreate",
    "AlertUpdate",
    "AlertResponse",
    "AlertListResponse",
    "AlertNotificationMappingCreate",
    "AlertNotificationMappingUpdate",
    "AlertCheckMappingCreate",
    "AlertCheckMappingUpdate",
    "AlertStatsResponse",
]
