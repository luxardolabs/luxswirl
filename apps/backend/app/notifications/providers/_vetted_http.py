"""The HTTP transport a provider posts through: its connections open only on an
address the SSRF settings allow.

A provider's URL is the operator's, like a check target. Its name is resolved once
per connection, off the event loop, and the socket opens on an address that lookup
vetted (`shared.ssrf_http.VettedTransport`), so a name that answers a safe address
when the provider is saved and the metadata address when it sends reaches nothing.
The cloud-metadata range is always refused here, whatever its setting says, as it
was before the settings applied to notifications; private networks follow
`security.block_private_networks`. No proxy from the server's environment is used:
it would resolve the name out of the settings' reach.
"""

from functools import partial

from app.schemas.setting_schema import NetworkProtection
from app.shared.ssrf import allowed_addresses
from app.shared.ssrf_http import VettedTransport


def vetted_transport(*, verify: bool, network: NetworkProtection) -> VettedTransport:
    """The transport a provider's client sends through under `network`."""
    return VettedTransport(
        verify=verify,
        allowed=partial(
            allowed_addresses,
            block_cloud_metadata=True,
            block_private_networks=network.block_private_networks,
        ),
    )
