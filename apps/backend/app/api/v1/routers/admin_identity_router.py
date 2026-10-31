"""
Admin API identity router - who the calling token acts as, and the API's own
OpenAPI document.
"""

import json

from fastapi import APIRouter, Request, Response

from app.api.deps import ApiUser
from app.core.admin_openapi import admin_openapi_schema
from app.core.config import settings
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.schemas.api_token_schema import ApiWhoAmIResponse
from app.services.core.api_token_core_service import ApiTokenCoreService

router = APIRouter(tags=["Admin API"])


@router.get(
    "/me",
    response_model=ApiWhoAmIResponse,
    summary="Who this token acts as",
    description="The calling API token's user, its role, and the token itself",
    name="api.admin.me.show",
)
@limiter.limit(
    settings.security.admin_api_rate_limit, key_func=api_token_rate_limit_key
)
async def admin_me(request: Request, principal: ApiUser) -> ApiWhoAmIResponse:
    return ApiTokenCoreService.who_am_i(principal)


@router.get("/openapi.json", include_in_schema=False, name="api.admin.openapi.show")
@limiter.limit(
    settings.security.admin_api_rate_limit, key_func=api_token_rate_limit_key
)
async def admin_openapi(request: Request, principal: ApiUser) -> Response:
    # The OpenAPI document itself: served as the JSON document it is.
    return Response(
        content=json.dumps(admin_openapi_schema(request.app)),
        media_type="application/json",
    )
