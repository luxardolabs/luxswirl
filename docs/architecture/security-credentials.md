# Agent Credential Encryption

## Overview

LuxSwirl agents automatically encrypt their API credentials at rest using **Fernet encryption** (AES-128-CBC + HMAC). This protects credentials stored in `/app/data/agent_credentials.json` from casual inspection, backup exposure, and unauthorized copying.

## How It Works

### Encryption Key Derivation

The encryption key is derived from two pieces of information from the container environment:

```
Encryption Key = PBKDF2-HMAC-SHA256(hostname + machine-id, 100,000 iterations)
```

**Components:**
- **Hostname:** Docker container hostname (from `socket.gethostname()`)
- **Machine ID:** Read from `/etc/machine-id` (Linux machine identifier)

**Key Properties:**
- Deterministic: Same hostname + machine-id = same key
- Machine-bound: Credentials cannot be decrypted on different hardware
- No secrets to manage: Automatically derived from environment

### Registration Flow

1. **First Run:** Agent registers with server using `LUXSWIRL_AUTH_KEY` (registration key)
2. **Credentials Issued:** Server generates unique `agent_id` + `api_key`
3. **Encryption:** Agent encrypts credentials using derived key
4. **Storage:** Encrypted blob saved to `/app/data/agent_credentials.json` (binary format)
5. **Subsequent Runs:** Agent loads and decrypts credentials automatically

### File Format

**Encrypted File:**
```
Binary Fernet token (not JSON-parseable)
File permissions: 0600 (owner read/write only)
```

**Legacy Plaintext (v0.x):**
```json
{
  "agent_id": "uuid-here",
  "api_key": "secret-key-here"
}
```
*Automatically migrated to encrypted format on first load*

## Docker Networking Modes

### Host Networking (Recommended for Stability)

```yaml
# compose.yaml
services:
  agent:
    image: luxswirl-agent:latest
    network_mode: host
    volumes:
      - agent_data:/app/data
```

**Hostname source:** Physical host machine's hostname **Machine ID source:** Host's `/etc/machine-id`

**Credential stability:**
- ✅ **Container rebuild:** Credentials decrypt successfully (same host)
- ✅ **Container upgrade:** Credentials persist across versions
- ❌ **Host machine renamed:** Credentials break (hostname changed)
- ❌ **Host reimaged:** Credentials break (new machine-id)

**Best for:** Production deployments where the agent should survive container updates

### Bridge Networking (Default Docker)

```yaml
# compose.yaml
services:
  agent:
    image: luxswirl-agent:latest
    container_name: luxswirl_agent_prod  # IMPORTANT: Use fixed name
    volumes:
      - agent_data:/app/data
```

**Hostname source:** Container name (if specified) or random ID **Machine ID source:** Container's `/etc/machine-id` (may be container-specific)

**Credential stability:**
- ✅ **Container rebuild (with `container_name`):** Usually works if machine-id is stable
- ⚠️ **Container rebuild (without `container_name`):** Credentials break (random hostname)
- ❌ **Different host machine:** Credentials break (different machine-id)

**Best for:** Development, non-production environments

### Summary Table

| Scenario | Host Networking | Bridge + `container_name` | Bridge (no name) |
|----------|----------------|---------------------------|------------------|
| Container restart | ✅ Works | ✅ Works | ✅ Works |
| Container rebuild | ✅ Works | ⚠️ Maybe | ❌ Breaks |
| New version | ✅ Works | ⚠️ Maybe | ❌ Breaks |
| Different host | ❌ Breaks | ❌ Breaks | ❌ Breaks |

## Security Properties

### What This Protects Against

✅ **Casual file inspection** - Credentials are not readable as plaintext ✅ **Backup exposure** - Encrypted files in backups cannot be easily decrypted ✅ **Log leaks** - Credentials automatically scrubbed from logs ✅ **Credential theft** - Cannot copy credentials to different machine

### What This Does NOT Protect Against

❌ **Attacker with container shell access** - Can read credentials from memory ❌ **Root access to host** - Can read machine-id and derive key ❌ **Memory dumps** - Decrypted credentials exist in RAM

### Threat Model

**Design Goals:**
- Protect credentials at rest from accidental exposure
- Prevent credential portability (machine-binding)
- Zero-configuration encryption (no secret management)

**Out of Scope:**
- Protection against root/privileged attackers
- Protection against memory forensics
- Enterprise HSM/KMS integration (see Future Enhancements)

## Troubleshooting

### Error: "Failed to decrypt credentials - encryption key may have changed"

**Causes:**
- Container rebuilt with different hostname
- Host machine hostname changed
- `/etc/machine-id` changed

**Solutions:**

1. **Delete credentials and re-register:**
   ```bash
   docker exec luxswirl_agent rm /app/data/agent_credentials.json
   docker compose restart agent
   ```
   Agent will detect missing credentials and re-register automatically.

2. **Use host networking mode** for better stability:
   ```yaml
   services:
     agent:
       network_mode: host
   ```

3. **Ensure fixed `container_name` in bridge mode:**
   ```yaml
   services:
     agent:
       container_name: luxswirl_agent_prod
   ```

### Inspecting Encrypted Credentials

**View file (binary):**
```bash
docker exec luxswirl_agent cat /app/data/agent_credentials.json
# Output: gAAAAABpEjRErHp5IaxG... (Fernet-encrypted binary)
```

**Temporarily disable encryption (TESTING ONLY):**
```yaml
environment:
  LUXSWIRL_DISABLE_CREDENTIAL_ENCRYPTION: "true"
```
⚠️ **Warning:** Credentials stored as plaintext JSON. Logs warning on startup.

### Migration from v0.x (Plaintext)

**Automatic Migration:**
- First load of plaintext file automatically encrypts it
- Original plaintext file is overwritten with encrypted version
- No manual intervention required

**Rollback to v0.x:**
- Not supported - encrypted files cannot be read by v0.x agents
- Must delete credentials and re-register if downgrading

## Configuration Options

### Environment Variables

```yaml
# Disable encryption (testing only - logs warning)
LUXSWIRL_DISABLE_CREDENTIAL_ENCRYPTION: "true"

# Allow insecure HTTP (testing only)
LUXSWIRL_ALLOW_INSECURE_HTTP: "true"
```

### Docker Compose Example (Production)

```yaml

services:
  agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent_prod
    network_mode: host  # Recommended for stability
    restart: unless-stopped

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000"
      LUXSWIRL_AUTH_KEY: "your-registration-key"

    volumes:
      - agent_data:/app/data  # Persistent credentials storage
      - /var/run/docker.sock:/var/run/docker.sock:ro  # For Docker checks (optional)

volumes:
  agent_data:
    driver: local
```

## Can Credentials Be Copied to Another Machine?

**Short Answer: NO**

**Scenario:** User copies encrypted credentials file to different machine with same `container_name`

**Result:**
- ✅ Same container hostname (from `container_name`)
- ❌ **Different machine-id** (from new host's `/etc/machine-id`)
- ❌ **Cannot decrypt** - encryption key derivation fails

**Security Implication:** This is **intentional** and provides credential theft protection. Credentials are bound to the specific hardware/VM they were created on.

**Migration Path:** To move an agent to new hardware, you must:
1. Deploy agent on new machine
2. Let it re-register (generates new credentials)
3. Approve new agent in server UI
4. Optionally: Copy check configurations to new agent

## Future Enhancements

Possible future work:

- **OS Keyring Integration:** Linux libsecret, macOS Keychain, Windows DPAPI
- **Credential Vault Support:** HashiCorp Vault, AWS Secrets Manager
- **External Encryption Keys:** User-provided encryption key via environment variable
- **KMS Integration:** Cloud KMS for enterprise deployments

## FAQ

**Q: Do I need to back up the credentials file?** A: No. If credentials are lost, the agent will re-register automatically. Just approve it in the server UI.

**Q: Can I use the same credentials file for multiple agents?** A: No. Each agent must have unique credentials. Attempting to copy will fail decryption due to machine-binding.

**Q: What happens if I change my Docker hostname?** A: Credentials will fail to decrypt. Delete the file and the agent will re-register.

**Q: Is this encryption FIPS-compliant?** A: No. Fernet uses AES-128-CBC which is not FIPS 140-2 validated. For compliance requirements, use external KMS.

**Q: Can I inspect the decrypted credentials?** A: Yes, temporarily disable encryption with `LUXSWIRL_DISABLE_CREDENTIAL_ENCRYPTION=true` (testing only).

**Q: What algorithm is used?** A: Fernet (AES-128 in CBC mode + HMAC-SHA256 for authentication). Key derived with PBKDF2-HMAC-SHA256, 100,000 iterations.

## Related Documentation

- [Agent Docker Deployment](../deployment/agent.md)
- [Security Policy](../../SECURITY.md)
- [Troubleshooting](../guides/troubleshooting.md)

## Support

For issues with credential encryption:
1. Check logs: `docker logs luxswirl_agent`
2. Verify networking mode and container_name in compose.yaml
3. Test with encryption disabled (temporarily)
4. Report issues: https://github.com/luxardolabs/luxswirl/issues
