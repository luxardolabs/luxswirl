"""
Models package - SQLAlchemy ORM models.
"""

from app.models.agent_metric_model import AgentMetric
from app.models.agent_model import Agent
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.alert_model import Alert
from app.models.alert_notification_mapping_model import AlertNotificationMapping
from app.models.base import Base, BaseModel, SoftDeleteMixin, TimestampMixin
from app.models.check_artifact_model import CheckArtifact
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.models.enum_model import (
    AgentApprovalStatus,
    AgentStatus,
    AlertTriggerType,
    CheckArtifactType,
    CheckErrorType,
    CheckHealthStatus,
    CheckType,
    JobStatus,
    JobType,
    MaintenanceJobKind,
    MaintenanceJobStatus,
    NotificationProviderType,
    NotificationStatus,
    SchedulerExecutionStatus,
    SchedulerJobCategory,
    SchedulerTriggerType,
    SettingCategory,
    UserRole,
)
from app.models.job_model import Job
from app.models.maintenance_job_model import MaintenanceJob
from app.models.notification_log_model import NotificationLog
from app.models.notification_provider_model import NotificationProvider
from app.models.registration_key_model import RegistrationKey
from app.models.scheduler_model import JobConfiguration, JobExecution
from app.models.session_model import Session
from app.models.setting_model import Setting
from app.models.status_page_model import StatusPage
from app.models.user_model import User

__all__ = [
    "Base",
    "BaseModel",
    "TimestampMixin",
    "SoftDeleteMixin",
    "Agent",
    "Check",
    "CheckArtifact",
    "CheckResult",
    "AgentMetric",
    "StatusPage",
    "NotificationProvider",
    "Alert",
    "AlertNotificationMapping",
    "AlertCheckMapping",
    "NotificationLog",
    "Job",
    "MaintenanceJob",
    "JobConfiguration",
    "JobExecution",
    "Setting",
    "RegistrationKey",
    "User",
    "Session",
    # Enums
    "AgentApprovalStatus",
    "AgentStatus",
    "AlertTriggerType",
    "CheckArtifactType",
    "CheckErrorType",
    "CheckHealthStatus",
    "CheckType",
    "JobStatus",
    "JobType",
    "MaintenanceJobKind",
    "MaintenanceJobStatus",
    "NotificationProviderType",
    "NotificationStatus",
    "SchedulerExecutionStatus",
    "SchedulerJobCategory",
    "SchedulerTriggerType",
    "SettingCategory",
    "UserRole",
]
