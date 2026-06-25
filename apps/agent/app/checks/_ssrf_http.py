"""SSRF-guarded httpx send, shared by the http and json checks.

httpx's own ``follow_redirects`` resolves and connects per hop but never re-checks
the SSRF policy, so a 3xx to ``http://169.254.169.254/...`` (or a DNS rebind) would
sail through. We drive redirects manually and validate ``request.url`` before each
hop — which also re-resolves at connect time, closing the create→fetch window.
"""

from typing import Any

import httpx
from shared.ssrf import assert_target_allowed


async def ssrf_guarded_send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, Any],
    body: Any = None,
    follow_redirects: bool,
    max_redirects: int = 20,
) -> httpx.Response:
    """Send ``method url`` through ``client``, validating the SSRF policy on the
    initial URL and every redirect hop. Raises ``shared.ssrf.SsrfBlockedError`` if a
    hop resolves into a blocked range (the http/json checks surface that as a failure)."""
    content = None if method.upper() in ("GET", "HEAD", "OPTIONS") else body
    request = client.build_request(method, url, headers=headers, content=content)
    limit = max_redirects if follow_redirects else 0
    for _ in range(limit):
        assert_target_allowed(str(request.url), block_cloud_metadata=True)
        response = await client.send(request, follow_redirects=False)
        if not response.is_redirect or response.next_request is None:
            return response
        request = response.next_request
    # Redirects disabled or limit reached — validate + issue the final hop.
    assert_target_allowed(str(request.url), block_cloud_metadata=True)
    return await client.send(request, follow_redirects=False)
