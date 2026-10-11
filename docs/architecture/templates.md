# LuxSwirl Template Organization Style Guide

## Overview

This document defines the official organization structure for all Jinja2 templates in the LuxSwirl web UI. Following this guide ensures consistency, maintainability, and clarity across the codebase.

## Template Types

### 1. Pages (`web/templates/pages/`)

**Definition:** Full HTML documents that extend `base.html` and represent complete views accessed by URL routes.

**Characteristics:**

- Always extend `base.html`
- Define the `{% block content %}` section
- Correspond to a URL route (e.g., `/agents` → `pages/agents.html`)
- Should be relatively lightweight - delegate complexity to partials, panels, and macros
- Include page-specific layout and structure only

**Naming Convention:** `{feature}.html` or `{feature}/{subpage}.html`

**Examples:**

```
pages/
├── status.html              # Main dashboard at /
├── agents.html              # Agents list at /agents
├── checks.html              # Checks list at /checks
└── settings/
    ├── index.html           # Settings landing at /settings
    ├── notifications.html   # Notifications page at /settings/notifications
    └── users.html           # Users page at /settings/users
```

**Best Practices:**

- Keep pages under 200 lines when possible
- Use partials for complex sections
- Use panels for side panel content
- Use macros for reusable components
- Each page should have a single, clear purpose

______________________________________________________________________

### 2. Partials (`web/templates/partials/`)

**Definition:** Reusable template fragments for specific sections of a page, loaded via HTMX or included directly.

**Characteristics:**

- Do NOT extend `base.html`
- Self-contained HTML fragments
- Often used as HTMX swap targets
- Can be table rows, cards, lists, or content sections
- May include some layout but primarily content-focused

**Naming Convention:** `{feature}_{component}.html`

**Organized by Feature:**

```
partials/
├── status_table.html         # Status dashboard table
├── status_summary.html       # Status summary stats
├── agents/
│   ├── agent_card.html       # Agent card component
│   └── agent_stats.html      # Agent statistics section
├── checks/
│   ├── check_row.html        # Single check row
│   └── check_history.html    # Check history section
└── settings/
    ├── setting_card.html     # Setting card display (HTMX updates)
    └── metrics_card.html     # Metrics configuration card
```

**Best Practices:**

- Partials should be independently renderable
- Include only minimal context requirements
- Document required template variables in comments at top
- Keep focused on single responsibility
- Ideal size: 50-200 lines

**Example Header Comment:**

```html
<!-- Partial: Agent Card
Required variables:
- agent: Agent model object
- current_user: User object (for permissions)
Optional variables:
- show_actions: bool (default: true)
-->
```

______________________________________________________________________

### 3. Panels (`web/templates/panels/`)

**Definition:** Side panel content for detail views, forms, and focused interactions.

**Characteristics:**

- Do NOT extend `base.html`
- Full-height side panel layouts
- Include header with close button
- Used with `#side-panel` HTMX target
- Contain forms, details, or workflows

**Naming Convention:** `{feature}_{action}_panel.html`

**Organized by Feature:**

```
panels/
├── agents/
│   ├── agent_create_panel.html
│   └── agent_edit_panel.html
├── checks/
│   ├── check_create_panel.html
│   ├── check_detail_panel.html       # Check detail view
│   └── check_edit_panel.html
├── notifications/
│   ├── provider_create_panel.html
│   └── provider_edit_panel.html
├── alerts/
│   ├── alert_create_panel.html
│   └── alert_edit_panel.html
├── registration_keys/
│   ├── key_create_panel.html
│   ├── key_created_panel.html   # Success display
│   └── agent_key_manage_panel.html
└── settings/
    ├── user_create_panel.html
    └── user_edit_panel.html
```

**Standard Panel Structure:**

```html
<div class="h-full w-full max-w-2xl bg-dark-bg-secondary border-l border-dark-border overflow-y-auto scrollbar-thin flex flex-col">
    <!-- Header -->
    <div class="sticky top-0 bg-dark-bg-secondary border-b border-dark-border px-6 py-4 z-10">
        <div class="flex items-center justify-between">
            <h2 class="text-xl font-semibold text-dark-text-primary">{Title}</h2>
            <button onclick="closeSidePanel()" class="p-2 hover:bg-dark-bg-tertiary rounded-lg transition-colors">
                {Close Icon}
            </button>
        </div>
    </div>

    <!-- Content -->
    <div class="flex-1 p-6">
        {Panel Content}
    </div>
</div>
```

**Best Practices:**

- Always include close button in header
- Use `closeSidePanel()` JavaScript function
- Keep panels focused on single task
- Max width typically `max-w-2xl` or `max-w-3xl`
- Use sticky header for scrollable content

______________________________________________________________________

### 4. Forms (`web/templates/forms/`)

**Definition:** Reusable form components that can be embedded in pages or panels.

**Characteristics:**

- Do NOT extend `base.html`
- Just the `<form>` element and its contents
- Can be used in pages, panels, or modals
- Include validation, error display, and field groups
- Use HTMX for submission when appropriate

**Naming Convention:** `{feature}_form.html`

**Organized by Feature:**

```
forms/
├── agents/
│   └── agent_form.html           # Agent create/edit form fields
├── checks/
│   └── check_form.html           # Check create/edit form fields
├── notifications/
│   └── provider_form.html        # Notification provider form
├── alerts/
│   └── alert_form.html           # Alert rule form
└── settings/
    ├── user_form.html            # User create/edit form
    └── registration_key_form.html # Registration key form
```

**Best Practices:**

- Forms should be reusable in different contexts (create vs edit)
- Use parameters to control behavior (e.g., `is_edit`, `submit_url`)
- Include inline validation hints
- Use consistent field styling via Tailwind classes
- Document required parameters

**Example Usage:**

```html
<!-- In a panel -->
<div class="flex-1 p-6">
    {% include 'forms/agents/agent_form.html' with context %}
</div>
```

______________________________________________________________________

### 5. Macros (`web/templates/macros/`)

**Definition:** Reusable template functions for common UI patterns and components.

**Characteristics:**

- Do NOT extend `base.html`
- Define reusable functions with `{% macro %}`
- Imported where needed with `{% import %}`
- Should be pure/stateless when possible
- Return HTML fragments

**Naming Convention:** `{feature}_macros.html`

**Organization:**

```
macros/
├── agents.html              # Agent-specific components
├── badges.html              # Base badge + status/role/category badges, tags, pips
├── buttons.html             # Button variants
├── cards.html               # Card components
├── charts.html              # Chart/sparkline components
├── filters.html             # Filter controls
├── form_fields.html         # Form field components
├── jobs.html                # Job-specific components
├── page.html                # Page-level layout helpers
├── panels.html              # Side panel components
├── settings.html            # Settings-specific macros
├── status.html              # Status badges/indicators (builds on badges.html)
└── tables.html              # Table components
```

> Note: there is no `macros/icons.html`. SVG icons live under `web/templates/icons/` (`icons/hero/` and `icons/weather/`), not in the macros directory.

**Example Macro File:**

```html
{# macros/settings.html #}

{% macro settings_nav(active_section, current_user) %}
<!-- Horizontal tab navigation -->
<div class="border-b border-dark-border">
    <nav class="flex space-x-6" aria-label="Settings tabs">
        <a href="/settings/notifications"
           class="{% if active_section == 'notifications' %}border-brand-500 text-brand-500{% else %}border-transparent text-dark-text-muted hover:text-dark-text-primary{% endif %} border-b-2 py-3 px-1 font-medium text-sm transition-colors">
            Notifications
        </a>
        {# More tabs... #}
    </nav>
</div>
{% endmacro %}

{% macro section_header(title, description, icon_svg) %}
<div class="mb-6">
    <div class="flex items-center gap-3 mb-2">
        {% if icon_svg %}
        <div class="w-10 h-10 rounded-lg bg-gradient-to-br from-brand-500/20 to-brand-600/20 border border-brand-500/30 flex items-center justify-center">
            {{ icon_svg|safe }}
        </div>
        {% endif %}
        <h2 class="text-2xl font-bold text-dark-text-primary">{{ title }}</h2>
    </div>
    {% if description %}
    <p class="text-sm text-dark-text-muted">{{ description }}</p>
    {% endif %}
</div>
{% endmacro %}
```

**Usage in Page:**

```html
{% from 'macros/settings.html' import settings_nav, section_header %}

{% block content %}
{{ section_header('Notifications', 'Configure how LuxSwirl sends alerts') }}
{{ settings_nav('notifications', current_user) }}
{# Rest of page... #}
{% endblock %}
```

**Best Practices:**

- Keep macros small and focused
- Document parameters in comments
- Avoid side effects (database calls, etc.)
- Use descriptive parameter names
- Group related macros in same file
- Export commonly used macros to avoid repetition

______________________________________________________________________

## Directory Structure

### Complete Template Organization

The tree under `apps/backend/app/templates/web/`. Each directory holds one role (enforced by `fw.template_role_placement`); the files themselves are the reference, so this lists directories, not every file.

```
templates/web/
├── layouts/          # bases pages extend (base.html, public_status.html)
├── macros/           # component macro libraries (buttons, cards, form_fields, …),
│                     #   catalogued at /settings/components
├── icons/            # inline SVGs ({% include "icons/hero/outline/x.svg" %})
├── pages/            # full pages: one per console screen
│   └── settings/     # ADMIN surface: settings, users, registration keys,
│                     #   scheduler, database health, components, logo demo
└── partials/         # HTMX swap targets and included fragments, grouped by domain
    │                 #   (agents/, checks/, jobs/, panels/, profile/, …)
    └── settings/     # ADMIN surface: setting cards, users, scheduler rows and
                      #   history, database health, import/export, registration-key panels
```

**Two surfaces.** Everything under `pages/settings/` and `partials/settings/` is rendered only by admin-gated routes; the rest of `pages/` and `partials/` is the console every signed-in user sees. Neither renders or includes the other's templates (`fw.surface_separation`, declared in `.luxarch.toml` `[target.surfaces]`). Markup both need lives in a macro: the error card is `error_card` in `macros/cards.html`, called by `partials/error_message.html` and `partials/settings/error_message.html`.

______________________________________________________________________

## Naming Conventions

### Files

| Type           | Convention                                  | Example                       |
| -------------- | ------------------------------------------- | ----------------------------- |
| Pages          | `{feature}.html`                            | `agents.html`                 |
| Pages (nested) | `{feature}/{subpage}.html`                  | `settings/notifications.html` |
| Partials       | `{feature}_{component}.html`                | `agent_card.html`             |
| Panels         | `{feature}_{action}_panel.html`             | `agent_create_panel.html`     |
| Forms          | `{feature}_form.html`                       | `agent_form.html`             |
| Macros         | `{feature}_macros.html` or `{feature}.html` | `settings.html`               |

### Macros

| Convention           | Example                                  |
| -------------------- | ---------------------------------------- |
| Nouns for components | `status_badge()`, `agent_card()`         |
| Verbs for actions    | `render_table()`, `format_timestamp()`   |
| Use underscores      | `section_header()` not `sectionHeader()` |

______________________________________________________________________

## When to Use Each Type

### Use a **Page** when:

- ✅ Content represents a distinct URL route
- ✅ User navigates to it via main navigation
- ✅ Needs full page layout with header/footer
- ✅ Has unique page title and breadcrumbs

### Use a **Partial** when:

- ✅ Content updates dynamically via HTMX
- ✅ Reused in multiple pages
- ✅ Represents a section/component of a page
- ✅ Doesn't need full page structure

### Use a **Panel** when:

- ✅ Content appears in side panel overlay
- ✅ Used for create/edit/detail workflows
- ✅ Needs header with close button
- ✅ Temporary focused view over main content

### Use a **Form** when:

- ✅ Form fields reused in multiple contexts
- ✅ Same form for create and edit operations
- ✅ Form embedded in pages or panels
- ✅ Complex field groups need isolation

### Use a **Macro** when:

- ✅ Small UI pattern used everywhere
- ✅ Needs parameters for customization
- ✅ Pure presentation logic (no data fetching)
- ✅ Reduces code duplication

______________________________________________________________________

## Best Practices

### General Principles

1. **Single Responsibility**: Each template should have one clear purpose
1. **DRY (Don't Repeat Yourself)**: Use macros and partials to avoid duplication
1. **Clear Dependencies**: Document required variables at the top of templates
1. **Consistent Styling**: Use Tailwind utility classes consistently
1. **Accessibility**: Include ARIA labels and semantic HTML
1. **Performance**: Keep templates lightweight, delegate complexity to Python services

### File Organization

1. **Group by Feature**: Use subdirectories for related templates (e.g., `agents/`, `checks/`)
1. **Limit Nesting**: Maximum 2 levels deep (e.g., `panels/agents/agent_create_panel.html`)
1. **Alphabetical Order**: Keep files alphabetically sorted within directories
1. **Avoid Orphans**: Every template should be referenced somewhere

### Code Style

1. **Indentation**: 4 spaces for HTML, 2 spaces for Jinja tags
1. **Comments**: Use `{# Comment #}` for Jinja comments, `<!-- Comment -->` for HTML
1. **Template Variables**: Document required variables at top of file
1. **Whitespace**: Blank line between major sections
1. **Line Length**: Aim for 120 characters or less

### HTMX Patterns

1. **Partials for Updates**: Use partials as HTMX swap targets
1. **Panels for Forms**: Use panels for side panel HTMX loads
1. **Consistent Targets**: Use standard IDs (`#side-panel`, `#main-content`, `#status-table`)
1. **Swap Strategy**: Use `innerHTML` for content replacement, `outerHTML` for row updates

______________________________________________________________________

## Migration Guide

### Moving Existing Templates

When refactoring existing templates to follow this guide:

1. **Identify Type**: Determine if it's a page, partial, panel, form, or should be a macro
1. **Create New Location**: Place in appropriate directory with correct naming
1. **Update References**: Update all imports, includes, and HTMX targets
1. **Test Thoroughly**: Verify all routes and HTMX interactions work
1. **Delete Old File**: Remove after confirming new structure works

### Example Migration

**Before:**

```
templates/
└── partials/
    └── notification_provider_form.html  # 11KB form in wrong place
```

**After:**

```
templates/
└── forms/
    └── notifications/
        └── provider_form.html           # Form in correct location
```

______________________________________________________________________

## Examples

### Example 1: Settings Page

**File:** `pages/settings/notifications.html`

```html
{% extends "base.html" %}
{% from 'macros/settings.html' import settings_nav, section_header %}

{% block content %}
<div class="mb-6">
    <h1 class="text-2xl font-bold mb-4">Settings</h1>
    {{ settings_nav('notifications', current_user) }}
</div>

{{ section_header(
    'Notification Providers',
    'Configure how LuxSwirl sends alerts • ' + total_providers|string + ' total providers'
) }}

<div class="flex justify-end mb-4">
    <button hx-get="/notification-providers/create-form"
            hx-target="#side-panel"
            class="btn-primary">
        Add Provider
    </button>
</div>

{% include 'partials/notifications/provider_list.html' %}
{% endblock %}
```

### Example 2: Creating a New Feature

When adding a new feature (e.g., "incidents"):

1. **Create page:** `pages/incidents.html`
1. **Create partials:** `partials/incidents/incident_card.html`, `incident_list.html`
1. **Create panels:** `panels/incidents/incident_create_panel.html`, `incident_detail_panel.html`
1. **Create form:** `forms/incidents/incident_form.html`
1. **Add macros:** Add to `macros/incidents.html` if needed
1. **Update router:** Add routes in `web/routers/incidents_router.py`

______________________________________________________________________

## Enforcement

### Code Review Checklist

- [ ] Template in correct directory for its type
- [ ] Naming convention followed
- [ ] Required variables documented
- [ ] No duplication that could use macros
- [ ] Consistent styling with existing templates
- [ ] HTMX targets use standard IDs
- [ ] Accessibility considerations included

### Linting (Future)

Consider adding automated checks:

- Template file placement validation
- Naming convention enforcement
- Unused template detection
- Required variable documentation check

______________________________________________________________________

## Related Documentation

- [Architecture Overview](overview.md) - Overall application architecture
- [Backend Layering](backend.md) - Router/service/CRUD chain
- [Database Schema](database.md) - Database schema and patterns

______________________________________________________________________

## Questions or Suggestions?

This is a living document. If you have questions about where something should go, or suggestions for improvements, open a GitHub issue.
