# LuxSwirl Synthetic Monitoring Guide

## Overview

LuxSwirl's synthetic monitoring uses Playwright to execute real browser automation scripts that simulate user interactions. Unlike simple ping or HTTP checks, synthetic checks can navigate through multi-page workflows, fill forms, click buttons, and verify complex user journeys.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Creating Synthetic Checks](#creating-synthetic-checks)
3. [Script Structure](#script-structure)
4. [Playwright API Reference](#playwright-api-reference)
5. [Performance Metrics](#performance-metrics)
6. [Artifacts](#artifacts)
7. [Examples](#examples)
8. [Best Practices](#best-practices)
9. [Troubleshooting](#troubleshooting)

---

## Architecture

### How Synthetic Checks Work

```
┌─────────────────────────────────────────────────────────────┐
│ Agent (Playwright Container)                                 │
│                                                              │
│  1. Load script from database                                │
│  2. Launch Chromium browser                                  │
│  3. Execute user script: async def run_check(page)           │
│  4. Capture screenshots & traces                             │
│  5. Upload results + artifacts to server                  │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Server (FastAPI Server)                                   │
│                                                              │
│  1. Receive check results                                    │
│  2. Store in TimescaleDB                                     │
│  3. Receive artifact uploads                                 │
│  4. Link artifacts via composite FK                          │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Web UI (HTMX + Tailwind)                                     │
│                                                              │
│  - View check history                                        │
│  - Display performance charts                                │
│  - Download screenshots/traces                               │
│  - Show per-step timing breakdown                            │
└─────────────────────────────────────────────────────────────┘
```

### Key Components

1. **Check Script** - Python async function stored in database
2. **Playwright Page** - Browser automation context
3. **Result Data** - Status, steps, errors, metrics
4. **Artifacts** - Screenshots, traces, custom data

---

## Creating Synthetic Checks

Create synthetic checks through the **REST API** or the **Checks UI** — these are the only supported paths, and both enforce the security controls: only an **admin** can create a synthetic check, the script is **AST-validated** before it's saved, and the attempt is audit-logged. The admin check is enforced in the core service, so neither path can bypass it. Note that the REST API has no role model — **any API Bearer token is treated as an administrator**, so scope your API tokens accordingly.

```bash
curl -X POST https://server.example.com:9000/api/v1/agents/{agent_id}/checks \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "my-synthetic-check",
    "check_type": "synthetic",
    "target": "https://example.com",
    "interval_seconds": 60,
    "timeout_seconds": 60,
    "script_code": "async def run_check(page):\n    ..."
  }'
```

**Fields:**
- `display_name` — unique name (kebab-case)
- `target` — the start URL
- `interval_seconds` — how often to run (60 = every minute)
- `timeout_seconds` — script execution timeout (per-check default 30s)
- `script_code` — your Playwright script (must define `async def run_check(page)`)

---

## Script Structure

### Basic Template

```python
async def run_check(page):
    """
    Main entry point for synthetic check.

    Args:
        page: Playwright Page object (already initialized)

    Returns:
        dict: Check result with status, steps, errors
    """
    steps = []
    failures = []

    try:
        # Your check logic here
        await page.goto('https://example.com')
        steps.append('✅ Loaded homepage')

        # Verify page title
        title = await page.title()
        if 'Example' in title:
            steps.append(f'✅ Page title verified: {title}')
        else:
            failures.append(f'Unexpected title: {title}')

    except Exception as e:
        failures.append(f'Check failed: {str(e)}')

    # Return result
    if failures:
        return {
            'status': 'failure',
            'steps': steps,
            'errors': failures
        }
    else:
        return {
            'status': 'success',
            'steps': steps
        }
```

### Required Return Structure

```python
{
    'status': 'success',  # or 'failure'
    'steps': [            # List of step descriptions
        '✅ Step 1 completed',
        '✅ Step 2 completed',
        '❌ Step 3 failed: error message'
    ],
    'errors': []          # Optional: list of error messages
}
```

### Optional Return Fields

```python
{
    'status': 'success',
    'steps': [...],

    # Custom metrics
    'custom_metric': 123.45,
    'items_processed': 10,

    # Timing data
    'step_durations': {'step1': 1.23, 'step2': 2.34},

    # Custom artifacts (screenshots, data files)
    'artifacts': [
        {
            'type': 'screenshot',
            'content_type': 'image/png',
            'filename': 'step1.png',
            'data': screenshot_bytes,
            'size_bytes': len(screenshot_bytes)
        }
    ]
}
```

---

## Playwright API Reference

### Common Page Methods

#### Navigation
```python
# Go to URL
await page.goto('https://example.com')
await page.goto('https://example.com', wait_until='networkidle')

# Navigate back/forward
await page.go_back()
await page.go_forward()

# Reload
await page.reload()
```

#### Waiting
```python
# Wait for page load states
await page.wait_for_load_state('load')        # Wait for 'load' event
await page.wait_for_load_state('domcontentloaded')
await page.wait_for_load_state('networkidle')  # No network activity for 500ms

# Wait for selector
await page.wait_for_selector('button.submit')
await page.wait_for_selector('button.submit', state='visible')

# Wait for timeout
await page.wait_for_timeout(1000)  # Wait 1 second
```

#### Finding Elements
```python
# By role (recommended - most reliable)
await page.get_by_role('button', name='Submit')
await page.get_by_role('link', name='Home')
await page.get_by_role('heading', name='Welcome')

# By text
await page.get_by_text('Click me')
await page.get_by_text('Click me', exact=True)

# By label (for form inputs)
await page.get_by_label('Email')
await page.get_by_label('Password')

# By placeholder
await page.get_by_placeholder('Enter email')

# By CSS selector
await page.locator('button.submit')
await page.locator('#login-button')

# By XPath
await page.locator('xpath=//button[@type="submit"]')
```

#### Interactions
```python
# Click
await page.get_by_role('button', name='Submit').click()
await page.locator('button.submit').click()

# Fill input
await page.get_by_label('Email').fill('user@example.com')
await page.locator('#email').fill('user@example.com')

# Type (simulates keypresses)
await page.get_by_label('Search').type('playwright', delay=100)

# Press keys
await page.keyboard.press('Enter')
await page.keyboard.press('Control+A')

# Select dropdown
await page.select_option('select#country', 'US')

# Check/uncheck
await page.get_by_label('Accept terms').check()
await page.get_by_label('Newsletter').uncheck()

# Hover
await page.get_by_role('button').hover()

# Drag and drop
await page.locator('#source').drag_to(page.locator('#target'))
```

#### Data Extraction
```python
# Get text content
title = await page.title()
heading = await page.locator('h1').text_content()

# Get attribute
href = await page.locator('a').get_attribute('href')

# Get inner HTML
html = await page.locator('div').inner_html()

# Check visibility
is_visible = await page.locator('button').is_visible()

# Count elements
count = await page.locator('li').count()
```

#### JavaScript Execution
```python
# Evaluate JavaScript
result = await page.evaluate('() => window.innerWidth')

# Get performance timing
timing = await page.evaluate("""() => {
    const t = performance.timing;
    return {
        domLoading: t.domLoading,
        domComplete: t.domComplete,
        loadEventEnd: t.loadEventEnd,
        navigationStart: t.navigationStart
    };
}""")
```

#### Screenshots
```python
# Full page screenshot
screenshot_bytes = await page.screenshot(full_page=True)

# Specific element
element = page.locator('div.chart')
screenshot_bytes = await element.screenshot()

# With options
screenshot_bytes = await page.screenshot(
    full_page=True,
    type='png'
)
```

---

## Performance Metrics

### Automatic Metrics

The agent automatically captures:

```python
{
    "browser_timing": {
        "navigationStart": 1762335022595,
        "domLoading": 1762335022628,
        "domComplete": 1762335022785,
        "loadEventEnd": 1762335022785,
        ...
    },
    "script_execution_time_ms": 13169.85,
    "total_execution_time_ms": 17039.78,
    "browser_load_time_ms": 190
}
```

### Custom Timing in Scripts

```python
import time

async def run_check(page):
    steps = []
    step_durations = {}

    # Time individual steps
    step_start = time.perf_counter()
    await page.goto('https://example.com')
    duration = round(time.perf_counter() - step_start, 2)
    step_durations['homepage_load'] = duration
    steps.append(f'✅ Homepage loaded in {duration}s')

    return {
        'status': 'success',
        'steps': steps,
        'step_durations': step_durations  # Will be shown in UI
    }
```

### Per-Step Browser Timing

```python
async def run_check(page):
    steps = []
    step_timings = {}

    for step_name in ['Home', 'About', 'Contact']:
        await page.get_by_role('link', name=step_name).click()
        await page.wait_for_load_state('networkidle')

        # Capture browser performance timing
        timing = await page.evaluate("""() => {
            const t = performance.timing;
            return {
                domLoading: t.domLoading,
                domComplete: t.domComplete,
                loadEventEnd: t.loadEventEnd,
                navigationStart: t.navigationStart
            };
        }""")

        step_timings[step_name] = timing
        steps.append(f'✅ {step_name} page loaded')

    return {
        'status': 'success',
        'steps': steps,
        'step_timings': step_timings  # Will show in UI table
    }
```

---

## Artifacts

### Automatic Artifacts

The agent automatically captures:
1. **Final screenshot** - Screenshot of last page state
2. **Playwright trace** - Complete execution trace with network, console, etc.

### Custom Artifacts

Return artifacts in your script:

```python
async def run_check(page):
    artifacts = []

    # Capture screenshot at specific point
    screenshot_bytes = await page.screenshot(full_page=True)
    artifacts.append({
        'type': 'screenshot',
        'content_type': 'image/png',
        'filename': 'login_page.png',
        'data': screenshot_bytes,
        'size_bytes': len(screenshot_bytes)
    })

    # Capture element screenshot
    chart = page.locator('div.chart')
    chart_bytes = await chart.screenshot()
    artifacts.append({
        'type': 'screenshot',
        'content_type': 'image/png',
        'filename': 'chart.png',
        'data': chart_bytes,
        'size_bytes': len(chart_bytes)
    })

    return {
        'status': 'success',
        'steps': ['✅ Captured screenshots'],
        'artifacts': artifacts
    }
```

### Artifact Types

- `screenshot` - PNG images
- `trace` - Playwright trace files (ZIP)

---

## Examples

### Example 1: Simple Homepage Check

```python
async def run_check(page):
    """Check that homepage loads and contains expected content."""
    steps = []

    # Load homepage
    await page.goto('https://example.com')
    await page.wait_for_load_state('networkidle')
    steps.append('✅ Homepage loaded')

    # Verify title
    title = await page.title()
    if 'Example Domain' in title:
        steps.append(f'✅ Title verified: {title}')
    else:
        return {
            'status': 'failure',
            'steps': steps,
            'errors': [f'Unexpected title: {title}']
        }

    # Check for key element
    heading = await page.locator('h1').text_content()
    steps.append(f'✅ Found heading: {heading}')

    return {'status': 'success', 'steps': steps}
```

### Example 2: Login Flow Check

```python
async def run_check(page):
    """Test user login functionality."""
    steps = []

    # Navigate to login page
    await page.goto('https://example.com/login')
    steps.append('✅ Loaded login page')

    # Fill login form
    await page.get_by_label('Email').fill('test@example.com')
    await page.get_by_label('Password').fill('testpassword123')
    steps.append('✅ Filled login form')

    # Submit form
    await page.get_by_role('button', name='Sign In').click()
    await page.wait_for_load_state('networkidle')
    steps.append('✅ Submitted login')

    # Verify successful login
    if await page.locator('text=Welcome back').is_visible():
        steps.append('✅ Login successful')
        return {'status': 'success', 'steps': steps}
    else:
        return {
            'status': 'failure',
            'steps': steps,
            'errors': ['Login failed - welcome message not found']
        }
```

### Example 3: E-commerce Checkout Flow

```python
async def run_check(page):
    """Test complete checkout flow."""
    import time

    steps = []
    step_durations = {}

    # 1. Browse catalog
    start = time.perf_counter()
    await page.goto('https://shop.example.com')
    await page.wait_for_load_state('networkidle')
    step_durations['catalog_load'] = round(time.perf_counter() - start, 2)
    steps.append('✅ Loaded product catalog')

    # 2. Search for product
    start = time.perf_counter()
    await page.get_by_placeholder('Search products').fill('laptop')
    await page.keyboard.press('Enter')
    await page.wait_for_load_state('networkidle')
    step_durations['search'] = round(time.perf_counter() - start, 2)
    steps.append('✅ Searched for product')

    # 3. View product details
    start = time.perf_counter()
    await page.get_by_role('link', name='MacBook Pro').first.click()
    await page.wait_for_load_state('networkidle')
    step_durations['product_view'] = round(time.perf_counter() - start, 2)
    steps.append('✅ Viewed product details')

    # 4. Add to cart
    start = time.perf_counter()
    await page.get_by_role('button', name='Add to Cart').click()
    await page.wait_for_selector('text=Added to cart')
    step_durations['add_to_cart'] = round(time.perf_counter() - start, 2)
    steps.append('✅ Added product to cart')

    # 5. View cart
    start = time.perf_counter()
    await page.get_by_role('link', name='Cart').click()
    await page.wait_for_load_state('networkidle')
    step_durations['cart_view'] = round(time.perf_counter() - start, 2)
    steps.append('✅ Viewed cart')

    # 6. Verify cart contents
    cart_items = await page.locator('.cart-item').count()
    if cart_items > 0:
        steps.append(f'✅ Cart contains {cart_items} item(s)')
    else:
        return {
            'status': 'failure',
            'steps': steps,
            'errors': ['Cart is empty'],
            'step_durations': step_durations
        }

    return {
        'status': 'success',
        'steps': steps,
        'step_durations': step_durations
    }
```

### Example 4: Multi-Step with Screenshots

```python
async def run_check(page):
    """Navigate through multiple pages and capture screenshots."""
    steps = []
    artifacts = []

    pages_to_check = ['Home', 'About', 'Products', 'Contact']

    for idx, page_name in enumerate(pages_to_check, 1):
        # Navigate to page
        await page.get_by_role('link', name=page_name).click()
        await page.wait_for_load_state('networkidle')

        # Capture screenshot
        screenshot_bytes = await page.screenshot(full_page=True)
        artifacts.append({
            'type': 'screenshot',
            'content_type': 'image/png',
            'filename': f'step_{idx:02d}_{page_name.lower()}.png',
            'data': screenshot_bytes,
            'size_bytes': len(screenshot_bytes)
        })

        steps.append(f'✅ {page_name} page loaded and captured')

    return {
        'status': 'success',
        'steps': steps,
        'artifacts': artifacts
    }
```

### Example 5: API Interaction Check

```python
async def run_check(page):
    """Check that API calls work correctly."""
    steps = []

    # Navigate to page that makes API calls
    await page.goto('https://dashboard.example.com')
    await page.wait_for_load_state('networkidle')
    steps.append('✅ Loaded dashboard')

    # Execute JavaScript to check API response
    api_data = await page.evaluate("""async () => {
        const response = await fetch('/api/stats');
        return await response.json();
    }""")

    if api_data and 'users' in api_data:
        user_count = api_data['users']
        steps.append(f'✅ API returned data: {user_count} users')
    else:
        return {
            'status': 'failure',
            'steps': steps,
            'errors': ['API did not return expected data']
        }

    return {
        'status': 'success',
        'steps': steps,
        'api_user_count': user_count  # Custom metric
    }
```

### Example 6: Form Submission with Validation

```python
async def run_check(page):
    """Test form submission and validation."""
    steps = []

    # Load form page
    await page.goto('https://example.com/contact')
    steps.append('✅ Loaded contact form')

    # Fill form
    await page.get_by_label('Name').fill('Test User')
    await page.get_by_label('Email').fill('test@example.com')
    await page.get_by_label('Message').fill('This is a test message')
    steps.append('✅ Filled form fields')

    # Submit
    await page.get_by_role('button', name='Send').click()
    steps.append('✅ Submitted form')

    # Wait for success message
    try:
        await page.wait_for_selector('text=Thank you', timeout=5000)
        steps.append('✅ Success message displayed')
        return {'status': 'success', 'steps': steps}
    except:
        return {
            'status': 'failure',
            'steps': steps,
            'errors': ['Success message not displayed after 5 seconds']
        }
```

---

## Best Practices

### 1. Use Semantic Selectors

**Good:**
```python
await page.get_by_role('button', name='Submit')
await page.get_by_label('Email')
await page.get_by_text('Welcome back')
```

**Avoid:**
```python
await page.locator('button.btn-primary.btn-lg')  # Fragile CSS
await page.locator('xpath=//div[3]/button[1]')   # Fragile XPath
```

### 2. Wait for Content

**Always wait for dynamic content:**
```python
await page.wait_for_load_state('networkidle')
await page.wait_for_selector('text=Loaded')
```

### 3. Handle Errors Gracefully

```python
try:
    await page.get_by_role('button', name='Optional Button').click()
    steps.append('✅ Clicked optional button')
except:
    steps.append('ℹ️ Optional button not present')
    # Continue execution
```

### 4. Provide Descriptive Steps

**Good:**
```python
steps.append('✅ Login form submitted successfully')
steps.append('✅ Dashboard loaded in 1.23s')
steps.append('❌ Product search returned 0 results')
```

**Avoid:**
```python
steps.append('Step 1 complete')
steps.append('Done')
```

### 5. Capture Relevant Metrics

```python
return {
    'status': 'success',
    'steps': steps,
    # Useful metrics
    'page_load_time_ms': load_time,
    'items_found': item_count,
    'cart_total': cart_value,
    # Per-step timing
    'step_durations': {...},
    'step_timings': {...}
}
```

### 6. Screenshot Strategic Points

```python
# Capture screenshots at decision points
await page.goto('/checkout')
screenshot_before = await page.screenshot()

await page.get_by_role('button', name='Place Order').click()
await page.wait_for_load_state('networkidle')

screenshot_after = await page.screenshot()
```

### 7. Set Appropriate Timeouts

```python
# For fast pages
timeout_seconds: 30

# For slow pages or multi-step checks
timeout_seconds: 60

# For very long workflows
timeout_seconds: 120
```

### 8. Keep Scripts Simple

- One check = one user journey
- Split complex workflows into multiple checks
- Avoid excessive conditionals

---

## Troubleshooting

### Script Doesn't Run

**Check:**
1. Script syntax is valid Python
2. Function name is exactly `run_check`
3. Function is async: `async def run_check(page):`
4. Returns a dictionary with `status` and `steps`

### Elements Not Found

**Solutions:**
```python
# Wait for element to appear
await page.wait_for_selector('button.submit', timeout=5000)

# Use more specific selectors
await page.get_by_role('button', name='Submit Form')  # Better than .locator('button')

# Check if element exists
if await page.locator('button.optional').is_visible():
    await page.locator('button.optional').click()
```

### Timeouts

**Increase timeout:**
```sql
UPDATE checks
SET timeout_seconds = 90
WHERE display_name = 'my-slow-check';
```

**Add explicit waits:**
```python
# Wait for specific condition
await page.wait_for_function('document.readyState === "complete"')
await page.wait_for_selector('text=Content loaded')
```

### Artifacts Not Uploading

**Ensure proper format:**
```python
artifacts.append({
    'type': 'screenshot',           # Required
    'content_type': 'image/png',    # Required
    'filename': 'step1.png',         # Required
    'data': screenshot_bytes,        # Required (bytes)
    'size_bytes': len(screenshot_bytes)  # Required
})
```

### Performance Issues

**Optimize:**
```python
# Use networkidle only when necessary
await page.wait_for_load_state('domcontentloaded')  # Faster than networkidle

# Avoid full-page screenshots
await page.screenshot()  # Just viewport

# Limit artifact count
# Only capture screenshots at key steps, not every page
```

---

## Additional Resources

- [Playwright Documentation](https://playwright.dev/python/docs/intro)
- [Playwright Selectors](https://playwright.dev/python/docs/selectors)
- [Playwright API Reference](https://playwright.dev/python/docs/api/class-page)

---

## Support

For questions or issues:
- Check agent logs: `docker logs luxswirl_agent`
- Check server logs: `docker logs luxswirl_server`
- Verify check configuration: `SELECT * FROM checks WHERE display_name = 'your-check'`
- Test Playwright script standalone before adding to LuxSwirl

---

