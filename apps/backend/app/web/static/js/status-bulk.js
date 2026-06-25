/**
 * Status page bulk selection and actions (ES6 Module)
 */

// Bulk selection state
let selectAllMode = false; // false = page only, true = all matching filters
let pageTotal, pageSize;
let deleteClickTime = 0; // Double-click tracker for delete

// Toggle selection mode
function toggleSelectionMode() {
    const checkboxes = document.querySelectorAll('.check-select');
    const bulkActions = document.getElementById('bulk-actions');
    const isHidden = checkboxes[0]?.classList.contains('hidden');

    checkboxes.forEach(cb => {
        if (isHidden) {
            cb.classList.remove('hidden');
        } else {
            cb.classList.add('hidden');
            cb.checked = false;
        }
    });

    if (isHidden) {
        bulkActions?.classList.remove('hidden');
        updateSelectionCount();
    } else {
        bulkActions?.classList.add('hidden');
        document.getElementById('modify-panel')?.classList.add('hidden');
        selectAllMode = false;
    }
}

// Update selection count
function updateSelectionCount() {
    const checkboxes = document.querySelectorAll('.check-select:not(#select-all-page)');
    const checked = document.querySelectorAll('.check-select:not(#select-all-page):checked');
    const selectionCount = document.getElementById('selection-count');
    const selectAllNotice = document.getElementById('select-all-notice');
    const selectAllLink = document.getElementById('select-all-link');
    const selectAllCheckbox = document.getElementById('select-all-page');

    if (checked.length > 0) {
        selectionCount.textContent = `${checked.length} selected`;
    } else {
        selectionCount.textContent = '';
    }

    // Show "select all X checks" notice if all visible are checked but there are more pages
    if (checked.length === checkboxes.length && checked.length > 0 && pageTotal > pageSize) {
        selectAllNotice.classList.remove('hidden');
        selectAllLink.textContent = `Select all ${pageTotal} checks`;
    } else {
        selectAllNotice.classList.add('hidden');
    }

    // Update select-all checkbox state
    if (checked.length === 0) {
        selectAllCheckbox.checked = false;
        selectAllCheckbox.indeterminate = false;
    } else if (checked.length === checkboxes.length) {
        selectAllCheckbox.checked = true;
        selectAllCheckbox.indeterminate = false;
    } else {
        selectAllCheckbox.checked = false;
        selectAllCheckbox.indeterminate = true;
    }
}

// Toggle select all on page
function toggleSelectAll(checked) {
    const checkboxes = document.querySelectorAll('.check-select:not(#select-all-page)');
    checkboxes.forEach(cb => {
        cb.checked = checked;
    });
    requestAnimationFrame(() => {
        updateSelectionCount();
    });
}

// Select all matching checks (across all pages)
function selectAllMatching() {
    selectAllMode = true;

    const selectionCount = document.getElementById('selection-count');
    const selectAllNotice = document.getElementById('select-all-notice');

    selectionCount.textContent = `All ${pageTotal} checks selected`;
    selectAllNotice.classList.add('hidden');

    // Check all visible boxes
    document.querySelectorAll('.check-select:not(#select-all-page)').forEach(cb => {
        cb.checked = true;
    });

    // Check the select-all checkbox
    const selectAllCheckbox = document.getElementById('select-all-page');
    if (selectAllCheckbox) {
        selectAllCheckbox.checked = true;
        selectAllCheckbox.indeterminate = false;
    }
}

// Clear selection
function clearSelection() {
    const checkboxes = document.querySelectorAll('.check-select');
    checkboxes.forEach(cb => cb.checked = false);
    const selectAllCheckbox = document.getElementById('select-all-page');
    if (selectAllCheckbox) {
        selectAllCheckbox.checked = false;
        selectAllCheckbox.indeterminate = false;
    }
    selectAllMode = false;
    updateSelectionCount();
}

// Get selected check IDs
function getSelectedIds() {
    if (selectAllMode) {
        return 'ALL';
    }
    const checkboxes = document.querySelectorAll('.check-select:not(#select-all-page):checked');
    return Array.from(checkboxes).map(cb => cb.dataset.checkId);
}

// Perform bulk action (enable, disable, delete)
function bulkAction(action) {
    const ids = getSelectedIds();
    const count = selectAllMode ? pageTotal : ids.length;

    if (count === 0) {
        return;
    }

    // For delete, require double-click within 2 seconds
    if (action === 'delete') {
        const now = Date.now();
        if (now - deleteClickTime < 2000) {
            // Double-click detected, proceed with delete
            deleteClickTime = 0;
        } else {
            // First click, set timer
            deleteClickTime = now;
            return;
        }
    }

    // Populate hidden form and submit via HTMX
    const form = document.getElementById('bulk-action-form');
    document.getElementById('bulk-action-type').value = action;
    document.getElementById('bulk-select-all').value = selectAllMode ? 'true' : 'false';

    // Add check IDs if not select-all mode
    const container = document.getElementById('bulk-check-ids-container');
    container.innerHTML = '';
    if (!selectAllMode) {
        ids.forEach(id => {
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = 'check_ids';
            input.value = id;
            container.appendChild(input);
        });
    }

    // Submit via HTMX (will handle HX-Trigger automatically)
    htmx.trigger(form, 'submit');
}

// Toggle modify panel
function toggleModifyPanel() {
    const panel = document.getElementById('modify-panel');
    if (panel) {
        panel.classList.toggle('hidden');
    }
}

// Bulk modify checks
function bulkModify() {
    const ids = getSelectedIds();
    const count = selectAllMode ? pageTotal : ids.length;

    if (count === 0) {
        return;
    }

    const form = document.getElementById('bulk-modify-form');

    // Check if any fields are filled (excluding hidden fields)
    const inputs = form.querySelectorAll('input[type="number"], select');
    let hasChanges = false;
    inputs.forEach(input => {
        if (input.value) hasChanges = true;
    });

    if (!hasChanges) {
        return;
    }

    // Set select_all hidden field
    document.getElementById('modify-select-all').value = selectAllMode ? 'true' : 'false';

    // Add check IDs if not select-all mode
    const container = document.getElementById('modify-check-ids-container');
    container.innerHTML = '';
    if (!selectAllMode) {
        ids.forEach(id => {
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = 'check_ids';
            input.value = id;
            container.appendChild(input);
        });
    }

    // Submit via HTMX (will handle HX-Trigger automatically)
    htmx.trigger(form, 'submit');
}

// Initialize module with event delegation
export function init(options) {
    pageTotal = options.total;
    pageSize = options.perPage;

    // Toggle selection mode button
    document.getElementById('toggle-selection-mode')?.addEventListener('click', toggleSelectionMode);

    // Select all page checkbox
    document.getElementById('select-all-page')?.addEventListener('change', (e) => {
        toggleSelectAll(e.target.checked);
    });

    // Select all matching link
    document.getElementById('select-all-link')?.addEventListener('click', (e) => {
        e.preventDefault();
        selectAllMatching();
    });

    // Individual checkbox changes
    document.addEventListener('change', (e) => {
        if (e.target.classList.contains('check-select') && e.target.id !== 'select-all-page') {
            updateSelectionCount();
        }
    });

    // Listen for bulk action completion (HTMX custom event)
    document.body.addEventListener('bulkActionComplete', () => {
        // Exit selection mode entirely (hides checkboxes and bulk actions toolbar)
        const checkboxes = document.querySelectorAll('.check-select');
        const bulkActions = document.getElementById('bulk-actions');
        const isSelectionMode = checkboxes[0] && !checkboxes[0].classList.contains('hidden');

        if (isSelectionMode) {
            // Hide checkboxes
            checkboxes.forEach(cb => {
                cb.classList.add('hidden');
                cb.checked = false;
            });

            // Hide bulk actions toolbar
            bulkActions?.classList.add('hidden');

            // Hide modify panel
            const modifyPanel = document.getElementById('modify-panel');
            modifyPanel?.classList.add('hidden');
            document.getElementById('bulk-modify-form')?.reset();

            // Reset state
            selectAllMode = false;
        }
    });

    // Event delegation for buttons
    document.addEventListener('click', (e) => {
        const target = e.target.closest('button');
        if (!target) return;

        // Bulk action buttons
        if (target.hasAttribute('data-bulk-action')) {
            e.preventDefault();
            bulkAction(target.dataset.bulkAction);
        }

        // Clear selection button
        if (target.hasAttribute('data-clear-selection')) {
            e.preventDefault();
            clearSelection();
        }

        // Toggle modify panel button
        if (target.hasAttribute('data-toggle-modify')) {
            e.preventDefault();
            toggleModifyPanel();
        }

        // Apply modifications button
        if (target.hasAttribute('data-apply-modifications')) {
            e.preventDefault();
            bulkModify();
        }

        // Cancel modifications button
        if (target.hasAttribute('data-cancel-modifications')) {
            e.preventDefault();
            toggleModifyPanel();
            document.getElementById('bulk-modify-form')?.reset();
        }
    });
}
