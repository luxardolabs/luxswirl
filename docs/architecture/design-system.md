# LuxSwirl Design System

This document defines the visual design standards for ALL UI components in LuxSwirl.

## 1. STATUS INDICATORS (Up/Down/Success/Error/etc)

**Standard Pattern:** Transparent background with colored border, medium rounded corners, NO ICONS

**Base Classes:**
```html
<span class="px-2 py-1 text-xs font-medium rounded-md bg-{color}-600/20 text-{color}-400 border border-{color}-600/30">
    Status Text
</span>
```

**Current Implementation:**
- **Up/Success**: `bg-green-600/20 text-green-400 border border-green-600/30`
- **Down/Error**: `bg-red-600/20 text-red-400 border border-red-600/30`
- **Unknown**: `bg-gray-600/20 text-gray-400 border border-gray-600/30`

**Key Features:**
- Transparent background (20% opacity)
- Colored text (400 shade)
- Border with 30% opacity
- Medium rounded corners (`rounded-md`)
- Text only - NO icons (removed checkmarks and X's)
- Small padding (`px-2 py-1`)
- Extra small text (`text-xs font-medium`)

**Reference Implementation:** `apps/backend/app/web/templates/macros/status.html` (`status_badge` macro). The base `badge` macro it builds on lives in `apps/backend/app/web/templates/macros/badges.html`.

---

## 2. TAGS (Agent tags, Check tags, etc)

**Standard Pattern:** Monospace font, transparent background (10% opacity), colored border, small rounded corners

**Base Classes:**
```html
<span class="px-2 py-0.5 text-xs font-mono rounded bg-{color}-600/10 text-{color}-300 border border-{color}-600/30">
    tag_name
</span>
```

**Current Implementation (Color-coded by type):**
- **Agent Tags**: `bg-blue-600/10 text-blue-300 border border-blue-600/30`
- **Check Tags**: `bg-green-600/10 text-green-300 border border-green-600/30`

**Key Features:**
- More transparent background than status badges (10% vs 20% opacity)
- Monospace font (`font-mono`) for technical/code-like appearance
- Colored text (300 shade - lighter than status badges)
- Border with 30% opacity
- Small rounded corners (`rounded`)
- Tighter vertical padding (`py-0.5` vs `py-1` for status badges)
- Extra small text (`text-xs`)
- Different colors by type (Agent=blue, Check=green)

**Hover States (for clickable tags):**
```html
<!-- Example from tag browser buttons -->
<button class="px-2 py-0.5 text-xs font-mono bg-blue-600/10 hover:bg-blue-600/20 text-blue-300 border border-blue-600/40 rounded transition-colors">
    tag_name
</button>
```

**Reference Implementation:**
- Display: `partials/status_table.html`
- Interactive: `panels/agents/fields/tags.html`

---

## 3. NOTIFICATION STATUS (Sent/Failed/Rate Limited/etc)

**Standard Pattern:** Matches status indicators exactly (Section 1)

**Current Implementation:**
- Same as status indicators - transparent background with colored border
- See Section 1 for complete pattern
- Examples: "Sent" (green), "Failed" (red), "Rate Limited" (yellow)

**Reference Implementation:** `pages/notification_logs.html` (notification status column)

---

## 4. CHECK TYPE BADGES (ping, http, tcp, etc)

**Standard Pattern:** Gray solid background, no border, medium rounded corners

**Base Classes:**
```html
<span class="px-2 py-1 text-xs font-medium rounded-md bg-dark-bg-tertiary text-dark-text-secondary">
    ping
</span>
```

**Current Implementation:**
- Solid gray background (`bg-dark-bg-tertiary`)
- Muted text color (`text-dark-text-secondary`)
- Medium rounded corners (`rounded-md`)
- Same padding as status badges (`px-2 py-1`)
- NO border (different from status badges and tags)
- All check types use the same gray color (not color-coded)

**Examples:**
- `ping`, `http`, `tcp`, `dns`, `json`, `mysql`, `postgres`, `synthetic`

**Note:** Different from status badges - check types are neutral/informational, not indicating success/failure, so they use consistent gray styling.

**Reference Implementation:** `apps/backend/app/web/templates/macros/badges.html` (`check_type_badge` macro)

---

## 5. ROLE BADGES (Admin/Editor/Viewer)

**Standard Pattern:** Same as status indicators, but with role-specific colors

**Base Classes:**
```html
<span class="px-2 py-1 text-xs font-medium rounded-md bg-{color}-600/20 text-{color}-400 border border-{color}-600/30">
    Role Name
</span>
```

**Current Implementation (Color-coded by role):**
- **Admin**: `bg-purple-600/20 text-purple-400 border border-purple-600/30`
- **Editor**: `bg-blue-600/20 text-blue-400 border border-blue-600/30`
- **Viewer**: `bg-gray-600/20 text-gray-400 border border-gray-600/30`

**Key Features:**
- Identical structure to status indicators (Section 1)
- Transparent background (20% opacity)
- Colored text (400 shade)
- Border with 30% opacity
- Medium rounded corners (`rounded-md`)
- Color-coded by role for visual hierarchy
- Purple for highest privilege (Admin), gray for lowest (Viewer)

**Reference Implementation:** `pages/settings/users.html`

---

## 6. ICON BUTTONS

**Standard Pattern:** Square buttons with opacity states (from network scan jobs)

**Base Pattern:**
```html
<button class="p-1.5 bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 rounded border border-blue-600/30 transition-colors">
    <svg class="w-3 h-3">...</svg>
</button>
```

**Disabled State (slightly opaque):**
```html
<button class="p-1.5 bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 rounded border border-blue-600/30 transition-colors opacity-30 cursor-not-allowed" disabled>
    <svg class="w-3 h-3">...</svg>
</button>
```

**Key Classes:**
- `p-1.5` - Square padding
- `bg-{color}-600/20` - Transparent colored background
- `hover:bg-{color}-600/30` - Slightly darker on hover
- `text-{color}-300` - Icon color
- `rounded` - Small rounded corners
- `border border-{color}-600/30` - Colored border matching background
- `transition-colors` - Smooth color transitions
- **Disabled:** `opacity-30 cursor-not-allowed` - Semi-transparent when not clickable
- **Enabled:** Full opacity (default) - Bright and clickable

**Color Variants (by action type):**
- **Blue (Edit)**: `bg-blue-600/20 text-blue-300 border-blue-600/30`
- **Green (View)**: `bg-green-600/20 text-green-300 border-green-600/30`
- **Red (Delete)**: `bg-red-600/20 text-red-300 border-red-600/30` *(see Delete Button Pattern below)*
- **Yellow (Cancel)**: `bg-yellow-600/20 text-yellow-300 border-yellow-600/30`
- **Purple (Clone/Manage)**: `bg-purple-600/20 text-purple-300 border-purple-600/30`
- **Orange (Reset)**: `bg-orange-600/20 text-orange-300 border-orange-600/30`

**Delete Button Pattern (Red with Pulsing Confirmation):**

Red delete buttons use a special double-click pattern with visual feedback:

```html
<!-- Delete button (idle state) -->
<button
    onclick="handleDeleteClick(event, this, '/checks/123', 'check-123')"
    data-resource-id="123"
    class="p-1.5 bg-red-600/20 hover:bg-red-600/30 text-red-300 rounded border border-red-600/30 transition-colors"
    title="Click twice to delete">
    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
              d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
    </svg>
</button>
```

**Delete Button States:**
- **Idle**: `bg-red-600/20 hover:bg-red-600/30` - Normal appearance
- **First Click (Waiting)**: `bg-red-600/50 hover:bg-red-600/60 scale-110 animate-pulse shadow-lg shadow-red-500/50` - **Pulsing with glowing halo**
- **Second Click (Executing)**: `opacity-50 cursor-not-allowed` - Disabled loading state
- **Timeout**: Reverts to idle after 2 seconds if no second click

**Key Features:**
- NO browser `confirm()` dialogs
- Visual feedback via pulsing glow with "light leak" shadow halo on first click
- 2-second timeout window for confirmation
- Smooth animations and state transitions
- Glowing shadow creates dramatic attention-grabbing effect
- Consistent with double-click pattern (Section 11)

**Implementation:** See `app/web/static/js/confirm-double-click.js`

**Example (Network Scan Jobs):**
```html
<!-- Enabled - Full color, clickable -->
<button class="p-1.5 bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 rounded border border-blue-600/30 transition-colors"
        hx-post="/jobs/network-scan/create" hx-vals='{"ip": "192.168.1.1"}'>
    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
    </svg>
</button>

<!-- Disabled - Opaque, not clickable -->
<button class="p-1.5 bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 rounded border border-blue-600/30 transition-colors opacity-30 cursor-not-allowed"
        disabled>
    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
    </svg>
</button>
```

**Reference Implementation:** `apps/backend/app/web/templates/partials/jobs/network_scan_detail.html`

---

## 7. BUTTON STYLES

**Standard Pattern:** Medium rounded corners with colored backgrounds

**Base Classes:**
```html
<button class="btn btn-{variant}">Button Text</button>
```

**Current Implementation:**
```css
/* Base button */
.btn {
  @apply inline-flex items-center justify-center px-4 py-2 rounded-md font-medium
         transition-colors duration-200 focus:outline-none focus:ring-2
         focus:ring-offset-2 focus:ring-offset-dark-bg-primary;
}

/* Variants */
.btn-primary   /* Blue - primary actions */
  @apply bg-brand-600 text-white hover:bg-brand-700 focus:ring-brand-500;

.btn-secondary /* Gray - secondary actions */
  @apply bg-dark-bg-tertiary text-dark-text-primary hover:bg-slate-600 focus:ring-slate-500;

.btn-danger    /* Red - destructive actions */
  @apply bg-status-error text-white hover:bg-red-700 focus:ring-red-500;

/* Sizes */
.btn-sm  /* Small: px-3 py-1.5 text-sm */
.btn-xs  /* Extra Small: px-2 py-1 text-xs */
```

**Key Features:**
- Medium rounded corners (`rounded-md`)
- Focus ring with offset for accessibility
- 200ms color transitions
- Icon support via inline flex
- Size variants for different contexts

**Reference Implementation:** `app/web/static/css/input.css`

---

## 8. CARD CORNERS

**Standard Pattern:** Large rounded corners on all cards

**Base Classes:**
```css
.card {
  @apply bg-dark-bg-secondary border border-dark-border rounded-lg shadow-sm;
}
```

**Current Implementation:**
- All cards use `rounded-lg` (larger radius than buttons)
- Shadow: `shadow-sm` for subtle depth
- Border: `border-dark-border` for definition
- Background: `bg-dark-bg-secondary`

**Variant:**
```css
.card-hover {
  @apply card transition-all duration-200 hover:shadow-md hover:border-brand-500/50;
}
```

**Reference Implementation:** `app/web/static/css/input.css`

---

## 9. TABLE STYLES

**Standard Pattern:** Consistent padding, hover effects, gray header background

**Base Classes:**
```css
.table {
  @apply w-full border-collapse;
}

.table thead {
  @apply bg-dark-bg-tertiary;
}

.table th {
  @apply px-6 py-3 text-left text-xs font-medium text-dark-text-secondary
         uppercase tracking-wider;
}

.table td {
  @apply px-6 py-4 text-sm text-dark-text-primary border-t border-dark-border;
}

.table tbody tr {
  @apply transition-colors duration-150 hover:bg-dark-bg-tertiary/50 cursor-pointer;
}
```

**Key Features:**
- Headers: `px-6 py-3` with gray background, uppercase text
- Cells: `px-6 py-4` with top border for row separation
- Hover: Semi-transparent tertiary background (50% opacity)
- Cursor: Pointer on rows (indicates clickability)
- Transition: 150ms smooth color change
- NO striped rows (clean, modern look)

**Important:**
- ALWAYS use `class="table"` on table element
- NO hardcoded classes on `<thead>`, `<tbody>`, `<th>`, or `<td>`
- CSS handles all styling automatically
- Only add width/alignment classes when needed (e.g., `class="w-12"`)

**Reference Implementation:** `app/web/static/css/input.css`

---

## 10. SIDE PANELS (Not Modals)

**Standard Pattern:** Slide-out panels from the right side of the screen

**Base Structure:**
```css
#side-panel {
  @apply fixed right-0 top-0 h-screen z-40 transition-transform duration-300;
}

#side-panel.hidden {
  @apply translate-x-full;
}
```

**Key Features:**
- Fixed positioning on right edge
- Full screen height
- High z-index (40) to overlay content
- 300ms slide animation using `translate-x-full`
- Panels used for: check details, agent edit, user edit, job details

**Pattern:**
```html
<div id="side-panel" class="hidden">
    <!-- Panel content slides in from right when 'hidden' class removed -->
    <div class="w-[600px] bg-dark-bg-secondary h-full shadow-2xl">
        <!-- Panel header with close button -->
        <button data-close-panel>Close</button>

        <!-- Panel content -->
    </div>
</div>
```

**Important:**
- Use side panels, NOT browser modals/dialogs
- No `confirm()` or `alert()` - use HTMX responses or double-click pattern instead

**Reference Implementation:** `app/web/static/css/input.css`

---

## 11. DOUBLE-CLICK DELETE CONFIRMATION

**Standard Pattern:** Click counter with 2-second timeout and pulsing glow feedback

**Purpose:** Replace browser `confirm()` dialogs with smooth, visual UX

**Implementation Pattern:**
```javascript
// Track click state per button
const deleteClickState = new Map();

function handleDeleteClick(event, button, deleteUrl, targetId = null) {
    event.preventDefault();
    event.stopPropagation();

    const buttonId = button.dataset.resourceId || deleteUrl;
    let state = deleteClickState.get(buttonId);

    if (!state) {
        state = { count: 0, timer: null, originalClasses: button.className };
        deleteClickState.set(buttonId, state);
    }

    state.count++;

    if (state.count === 1) {
        // First click - show pulsing glow feedback
        clearTimeout(state.timer);

        button.classList.remove('bg-red-600/20', 'hover:bg-red-600/30');
        button.classList.add('bg-red-600/50', 'hover:bg-red-600/60', 'scale-110', 'animate-pulse', 'shadow-lg', 'shadow-red-500/50');
        button.title = 'Click again to confirm delete';

        // Reset after 2 seconds if no second click
        state.timer = setTimeout(() => {
            button.className = state.originalClasses;
            button.title = 'Delete';
            state.count = 0;
        }, 2000);

    } else if (state.count === 2) {
        // Second click - execute delete
        clearTimeout(state.timer);

        button.disabled = true;
        button.classList.remove('bg-red-600/50', 'hover:bg-red-600/60', 'scale-110', 'animate-pulse', 'shadow-lg', 'shadow-red-500/50');
        button.classList.add('opacity-50', 'cursor-not-allowed');
        button.title = 'Deleting...';

        // Execute delete via fetch
        fetch(deleteUrl, { method: 'DELETE' })
            .then(response => {
                if (response.ok && targetId) {
                    const target = document.getElementById(targetId);
                    if (target) {
                        target.style.transition = 'opacity 300ms';
                        target.style.opacity = '0';
                        setTimeout(() => target.remove(), 300);
                    }
                }
            });

        state.count = 0;
        deleteClickState.delete(buttonId);
    }
}
```

**Key Features:**
- **2-second timeout** (not 300ms) - gives user time to see feedback
- **Pulsing glow feedback** on first click (`animate-pulse` + `scale-110` + brighter red + `shadow-lg shadow-red-500/50`)
- **"Light leak" effect** - Glowing red halo around button creates dramatic visual attention
- Visual state progression: Idle → Pulsing with glow (waiting) → Loading (executing)
- No browser dialogs - smooth in-app experience
- Per-button state tracking (Map-based)
- Automatic cleanup on completion

**Visual Feedback States:**
1. **Idle**: Normal red button appearance
2. **First Click**: Brighter red + scale up + **pulsing animation + glowing shadow halo**
3. **Second Click**: Disabled state with opacity (glow removed)
4. **Timeout**: Reverts to idle if no second click

**Current Status:**
- ✅ **Fully Implemented**: All delete buttons use this pattern
- ✅ Pulsing glow feedback on all delete confirmations
- ✅ NO browser `confirm()` dialogs anywhere

**Browser Confirms to Remove:**
```html
<!-- OLD - Browser modal -->
<button hx-confirm="Are you sure?">Delete</button>
<form onsubmit="return confirm('Are you sure?')">

<!-- NEW - Double-click pattern with pulsing glow -->
<button onclick="handleDeleteClick(event, this, '/resource/123', 'resource-123')"
        data-resource-id="123"
        class="p-1.5 bg-red-600/20 hover:bg-red-600/30 text-red-300 rounded border border-red-600/30 transition-colors">
    <svg class="w-3.5 h-3.5">...</svg>
</button>
```

**Reference Implementation:** `app/web/static/js/confirm-double-click.js` (ES6 module)

---

## 12. ACTION FEEDBACK

**Standard Pattern:** All user actions MUST have visible feedback

**Required Feedback Types:**
1. **Success actions**: Toast notification or inline success message
2. **Error actions**: Toast notification or inline error message
3. **Loading states**: Spinner, skeleton, or disabled button state
4. **Delete actions**: Use double-click pattern (Section 11) with visual feedback

**Toast Pattern:**
```javascript
// Show success toast
showToast('Check deleted successfully', 'success');

// Show error toast
showToast('Failed to delete check', 'error');

// Show loading state
button.disabled = true;
button.innerHTML = '<spinner> Deleting...';
```

---

## IMPLEMENTATION COMPLETE

All design patterns documented above represent the current LuxSwirl design system.

**Completed Sections:**
1. ✅ Status Indicators - Transparent with border, no icons
2. ✅ Tags - Monospace, color-coded (Agent=blue, Check=green)
3. ✅ Notification Status - Same as status indicators
4. ✅ Check Type Badges - Gray solid, no border
5. ✅ Role Badges - Color-coded (Admin=purple, Editor=blue, Viewer=gray)
6. ✅ Icon Buttons - Square with opacity states, color-coded by action
7. ✅ Button Styles - Primary, secondary, danger variants
8. ✅ Card Corners - rounded-lg on all cards
9. ✅ Table Styles - Consistent padding, hover effects
10. ✅ Side Panels - Slide-out from right (not modals)
11. ✅ Double-Click Delete - **Fully implemented with pulsing glow feedback**
12. ✅ Action Feedback - Toast notifications for settings save/reset

**Next Steps:**
1. Apply these patterns consistently across all new components
2. Continue using toast notifications for async actions
3. Maintain the square icon button pattern for all new action buttons
