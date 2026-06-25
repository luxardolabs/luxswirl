"""
DNS Check Module - Implements DNS query health checks.
"""

from typing import Any

from shared.ssrf import assert_target_allowed

from app.checks.base import BaseCheck

try:
    import dns.asyncresolver
    import dns.exception
    import dns.rdatatype

    DNS_AVAILABLE = True
except ImportError:
    DNS_AVAILABLE = False


class DNSCheck(BaseCheck):
    """Check for DNS resolution and record validation."""

    VALID_RECORD_TYPES = [
        "A",
        "AAAA",
        "CNAME",
        "MX",
        "TXT",
        "NS",
        "PTR",
        "SOA",
        "SRV",
        "CAA",
    ]

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate DNS-specific configuration.

        Args:
            config: The check configuration to validate

        Raises:
            ValueError: If required configuration fields are missing
        """
        super().validate_config(config)

        if not DNS_AVAILABLE:
            raise ValueError(
                "DNS check requires dnspython library. Install with: pip install dnspython"
            )

        if "record_type" not in config:
            raise ValueError(f"DNS check {config.get('name', 'unnamed')} must have a 'record_type'")

        record_type = config["record_type"].upper()
        if record_type not in self.VALID_RECORD_TYPES:
            raise ValueError(
                f"Invalid DNS record type '{record_type}'. Must be one of: {', '.join(self.VALID_RECORD_TYPES)}"
            )

    async def run(self) -> dict[str, Any]:
        """Execute the DNS query check.

        Returns:
            A dictionary containing the check result
        """
        domain = self.config["target"]
        record_type = self.config["record_type"].upper()
        timeout = self.config.get("timeout", 5)
        retries = self.config.get("retries", 1)

        # Optional config parameters
        nameserver = self.config.get("nameserver", "1.1.1.1")  # Default to Cloudflare
        port = self.config.get("port", 53)  # Default DNS port
        expect_value = self.config.get("expect_value")  # Expected record value
        expect_count = self.config.get("expect_count")  # Expected number of records

        success = False
        latency_ms = None
        error = None
        records = []
        additional_data = {
            "nameserver": nameserver,
            "port": port,
        }

        # SSRF: the nameserver is the host the agent actually connects to — block
        # one that resolves into the cloud-metadata range.
        assert_target_allowed(nameserver, block_cloud_metadata=True)

        # Try query with retries
        for _attempt in range(retries):
            try:
                start_time = self.start_timer()

                # Create resolver
                resolver = dns.asyncresolver.Resolver()
                resolver.timeout = timeout
                resolver.lifetime = timeout

                # Configure nameserver with port
                resolver.nameservers = [nameserver]
                resolver.port = port

                # Perform DNS query
                rdtype = dns.rdatatype.from_text(record_type)
                answers = await resolver.resolve(domain, rdtype)

                # Calculate latency
                latency_ms = self.stop_timer(start_time)

                # Extract records based on type
                records = self._extract_records(answers, record_type)
                additional_data["record_count"] = len(records)
                additional_data["records"] = records

                # Add DNS-specific performance and metadata
                if answers.response:
                    resp = answers.response
                    additional_data["ttl"] = int(answers.rrset.ttl) if answers.rrset else None
                    additional_data["authoritative"] = bool(resp.flags & dns.flags.AA)
                    additional_data["truncated"] = bool(resp.flags & dns.flags.TC)
                    additional_data["recursion_desired"] = bool(resp.flags & dns.flags.RD)
                    additional_data["recursion_available"] = bool(resp.flags & dns.flags.RA)

                # Add canonical name if CNAME chain exists
                if answers.canonical_name != answers.qname:
                    additional_data["canonical_name"] = str(answers.canonical_name)

                # Validate expected value if specified
                if expect_value:
                    if not self._validate_expect_value(records, expect_value):
                        error = f"DNS record does not contain expected value: {expect_value}"
                        continue

                # Validate expected count if specified
                if expect_count is not None:
                    if len(records) != expect_count:
                        error = f"Expected {expect_count} records but got {len(records)}"
                        continue

                # If we get here, the check succeeded
                success = True
                break

            except dns.resolver.NXDOMAIN:
                error = f"Domain does not exist: {domain}"
                continue
            except dns.resolver.NoAnswer:
                error = f"No {record_type} records found for {domain}"
                continue
            except dns.resolver.NoNameservers:
                error = "No nameservers available to answer the query"
                continue
            except dns.exception.Timeout:
                error = f"DNS query timed out after {timeout}s"
                continue
            except dns.exception.DNSException as e:
                error = f"DNS error: {str(e)}"
                continue
            except Exception as e:
                error = f"Unexpected error: {str(e)}"
                continue

        # Create the result with DNS data in metrics
        return self.create_result(
            success=success,
            latency_ms=latency_ms,
            error=error,
            metrics={
                "dns": {
                    "target_domain": domain,
                    "record_type": record_type,
                    **additional_data,  # nameserver, port, record_count, records
                }
            },
        )

    def _extract_records(self, answers, record_type: str) -> list[str]:
        """Extract DNS records from answer based on record type.

        Args:
            answers: DNS answer object
            record_type: The type of record (A, AAAA, CNAME, etc.)

        Returns:
            List of record values as strings
        """
        records = []

        for rdata in answers:
            if record_type == "A":
                records.append(str(rdata.address))
            elif record_type == "AAAA":
                records.append(str(rdata.address))
            elif record_type == "CNAME":
                records.append(str(rdata.target))
            elif record_type == "MX":
                records.append(f"{rdata.preference} {rdata.exchange}")
            elif record_type == "TXT":
                # TXT records can have multiple strings
                txt_value = " ".join([s.decode() for s in rdata.strings])
                records.append(txt_value)
            elif record_type == "NS":
                records.append(str(rdata.target))
            elif record_type == "PTR":
                records.append(str(rdata.target))
            elif record_type == "SOA":
                records.append(
                    f"{rdata.mname} {rdata.rname} {rdata.serial} {rdata.refresh} {rdata.retry} {rdata.expire} {rdata.minimum}"
                )
            elif record_type == "SRV":
                records.append(f"{rdata.priority} {rdata.weight} {rdata.port} {rdata.target}")
            elif record_type == "CAA":
                records.append(f'{rdata.flags} {rdata.tag} "{rdata.value}"')
            else:
                # Fallback for other types
                records.append(str(rdata))

        return records

    def _validate_expect_value(self, records: list[str], expect_value: str) -> bool:
        """Check if any record contains the expected value.

        Args:
            records: List of DNS record values
            expect_value: The expected value to find

        Returns:
            True if expected value found in any record, False otherwise
        """
        expect_value_lower = expect_value.lower()

        for record in records:
            if expect_value_lower in record.lower():
                return True

        return False
