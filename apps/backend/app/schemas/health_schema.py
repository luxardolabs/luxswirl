"""Response contracts for the unauthenticated service metadata routes."""

from typing import Literal

from pydantic import Field

from app.schemas.base_schema import BaseSchema

Environment = Literal["development", "staging", "production"]


class HealthResponse(BaseSchema):
    """`GET /health`: liveness, plus which build and deployment answered."""

    status: Literal["healthy"] = Field(..., description="Always `healthy` when served")
    version: str = Field(..., description="The running build's VERSION")
    environment: Environment = Field(..., description="Deployment environment")


class ApiInfoResponse(BaseSchema):
    """`GET /api`: what this API is and where its docs live."""

    name: str = Field(..., description="Service name")
    description: str = Field(..., description="One-line description")
    version: str = Field(..., description="The running build's VERSION")
    docs: str | None = Field(
        None, description="Path of the interactive docs; absent in production"
    )
