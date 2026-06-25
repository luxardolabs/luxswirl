/**
 * Dashboard management — drag-and-drop, inline rename, tag filtering, and
 * inline filter inputs for the status-pages /manage view.
 *
 * Server roundtrips all go through `htmx.ajax(...)` so HTMX owns the swap
 * lifecycle (hx-confirm dialogs, htmx:afterSwap re-binding, indicators).
 * Pure-client JS lives here only for things that have no server roundtrip:
 * Sortable.js drag visuals, the inline rename input lifecycle, debounced
 * filter inputs that flush to the server, and the available-checks-card
 * "added/not added" visual indicator.
 */

let sortableInstance = null;
let selectedTags = [];
let checkActionsInitialized = false;


/**
 * Initialize Sortable.js for drag-and-drop reordering. The drag visuals are
 * pure client-side; the persist call (saveItemOrder) goes through htmx.ajax.
 */
function initializeSortable() {
    const dashboardItems = document.getElementById('dashboard-items');
    if (!dashboardItems) return;

    if (sortableInstance) {
        sortableInstance.destroy();
    }

    sortableInstance = Sortable.create(dashboardItems, {
        animation: 150,
        handle: '.drag-handle',
        ghostClass: 'sortable-ghost',
        chosenClass: 'sortable-chosen',
        dragClass: 'sortable-drag',
        group: 'dashboard',
        onEnd: function(evt) {
            if (evt.oldIndex !== evt.newIndex) {
                saveItemOrder();
            }
        }
    });

    document.querySelectorAll('.group-checks-container').forEach(container => {
        Sortable.create(container, {
            animation: 150,
            handle: '.drag-handle',
            ghostClass: 'sortable-ghost',
            chosenClass: 'sortable-chosen',
            dragClass: 'sortable-drag',
            group: 'dashboard',
            onEnd: () => saveItemOrder()
        });
    });
}

/**
 * Persist the current item order. Computes the new layout from DOM state,
 * sends it to /reorder as a JSON-encoded form field, and lets HTMX swap
 * the refreshed dashboard_items partial back into #dashboard-items. The
 * htmx:afterSwap listener then re-runs initializeSortable + initializeCheckActions.
 */
function saveItemOrder() {
    const dashboardItems = document.getElementById('dashboard-items');
    const statusPageId = dashboardItems.dataset.statusPageId;

    const items = [];
    const topLevelItems = dashboardItems.querySelectorAll(':scope > .dashboard-item');

    topLevelItems.forEach((el, index) => {
        const itemType = el.dataset.itemType;
        if (itemType === 'group') {
            const groupContainer = el.querySelector('.group-checks-container');
            const groupChecks = groupContainer
                ? Array.from(groupContainer.querySelectorAll('[data-check-id]'))
                : [];
            items.push({
                oldIndex: parseInt(el.dataset.itemIndex),
                newIndex: index,
                type: 'group',
                checks: groupChecks.map(checkEl => checkEl.dataset.checkId)
            });
        } else if (itemType === 'check') {
            items.push({
                oldIndex: parseInt(el.dataset.itemIndex),
                newIndex: index,
                type: 'check',
                checkId: el.dataset.checkId
            });
        }
    });

    htmx.ajax('POST', `/status-pages/${statusPageId}/reorder`, {
        target: '#dashboard-items',
        swap: 'innerHTML',
        values: { items_json: JSON.stringify(items) },
    });
}

/**
 * Initialize tag filtering functionality
 */
function initializeTagFiltering() {
    const tagSelect = document.getElementById('filter-tag');
    const selectedTagsContainer = document.getElementById('selected-tags');
    const hiddenTagsInput = document.getElementById('filter-tags-hidden');

    if (!tagSelect) return;

    function renderSelectedTags() {
        if (!selectedTagsContainer) return;
        selectedTagsContainer.innerHTML = '';
        selectedTags.forEach(tag => {
            const pill = document.createElement('span');
            pill.className = 'inline-flex items-center gap-1 px-2 py-1 bg-brand-600/20 text-brand-300 rounded-full text-xs border border-brand-500/30';
            pill.innerHTML = `
                ${tag}
                <button type="button" class="remove-tag-btn hover:text-white" data-tag="${tag}">
                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                    </svg>
                </button>
            `;
            selectedTagsContainer.appendChild(pill);
        });
        if (hiddenTagsInput) {
            hiddenTagsInput.value = selectedTags.join(',');
        }
    }

    tagSelect.addEventListener('change', () => {
        const tag = tagSelect.value;
        if (tag && !selectedTags.includes(tag)) {
            selectedTags.push(tag);
            renderSelectedTags();
            if (hiddenTagsInput) {
                htmx.trigger(hiddenTagsInput, 'change');
            }
        }
        tagSelect.value = '';
    });

    document.body.addEventListener('click', (e) => {
        const removeBtn = e.target.closest('.remove-tag-btn');
        if (!removeBtn) return;
        const tag = removeBtn.dataset.tag;
        selectedTags = selectedTags.filter(t => t !== tag);
        renderSelectedTags();
        if (hiddenTagsInput) {
            htmx.trigger(hiddenTagsInput, 'change');
        }
    });

    // Initialize from URL params on page load
    const urlParams = new URLSearchParams(window.location.search);
    const tagsParam = urlParams.get('tags');
    if (tagsParam) {
        selectedTags = tagsParam.split(',').filter(t => t.trim());
        renderSelectedTags();
    }
}

/**
 * Initialize check action buttons (add to dashboard).
 * Uses event delegation since dashboard items are dynamically loaded.
 */
function initializeCheckActions() {
    if (checkActionsInitialized) return;
    checkActionsInitialized = true;

    document.body.addEventListener('click', (e) => {
        const addBtn = e.target.closest('.add-check-btn');
        if (addBtn) {
            const checkEl = addBtn.closest('[data-check-id]');
            if (checkEl) {
                addCheckToDashboard(checkEl);
            }
            return;
        }

        const renameBtn = e.target.closest('.rename-group-btn');
        if (renameBtn) {
            e.stopPropagation();
            const itemIndex = parseInt(renameBtn.dataset.itemIndex);
            renameGroup(itemIndex);
            return;
        }
    });
}

/**
 * Add a check to the dashboard via HTMX. Server returns the refreshed
 * dashboard_items partial; we then mark the available-checks card as
 * "added" purely client-side (no extra roundtrip).
 */
function addCheckToDashboard(checkEl) {
    const dashboardItems = document.getElementById('dashboard-items');
    const statusPageId = dashboardItems.dataset.statusPageId;
    const checkId = checkEl.dataset.checkId;

    htmx.ajax('POST', `/status-pages/${statusPageId}/add-check`, {
        target: '#dashboard-items',
        swap: 'innerHTML',
        values: { check_id: checkId },
    }).then(() => {
        const emptyState = document.getElementById('empty-state');
        if (emptyState) {
            emptyState.remove();
        }
        updateCheckCardVisual(checkEl, true);
    });
}

/**
 * Update a check card's visual state in the available-checks list.
 */
function updateCheckCardVisual(checkEl, isAdded) {
    const addBtn = checkEl.querySelector('.add-check-btn');

    if (isAdded) {
        checkEl.classList.remove('bg-dark-bg-tertiary', 'hover:bg-dark-bg-secondary');
        checkEl.classList.add('bg-dark-bg-secondary', 'opacity-60');
        if (addBtn) {
            addBtn.classList.remove('text-brand-400', 'hover:text-brand-300');
            addBtn.classList.add('text-green-500');
            addBtn.title = 'Already on dashboard';
            addBtn.innerHTML = `
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
                </svg>
            `;
        }
    } else {
        checkEl.classList.remove('bg-dark-bg-secondary', 'opacity-60');
        checkEl.classList.add('bg-dark-bg-tertiary', 'hover:bg-dark-bg-secondary');
        if (addBtn) {
            addBtn.classList.remove('text-green-500');
            addBtn.classList.add('text-brand-400', 'hover:text-brand-300');
            addBtn.title = 'Add to dashboard';
            addBtn.innerHTML = `
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6"/>
                </svg>
            `;
        }
    }
}

/**
 * Rename a group via inline editing. Pure client-side input lifecycle;
 * the save call goes through htmx.ajax.
 */
function renameGroup(itemIndex) {
    const groupElement = document.querySelector(`.dashboard-item[data-item-index="${itemIndex}"]`);
    if (!groupElement) return;

    const nameSpan = groupElement.querySelector('.group-name');
    if (!nameSpan) return;

    const currentName = nameSpan.textContent;

    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentName;
    input.className = 'input text-sm font-medium px-2 py-0.5 w-48';

    nameSpan.replaceWith(input);
    input.focus();
    input.select();

    const revertToSpan = () => {
        const span = document.createElement('span');
        span.className = 'font-medium text-dark-text-primary group-name';
        span.textContent = currentName;
        input.replaceWith(span);
    };

    const saveRename = () => {
        const newName = input.value.trim();
        if (!newName || newName === currentName) {
            revertToSpan();
            return;
        }
        const dashboardItems = document.getElementById('dashboard-items');
        const statusPageId = dashboardItems.dataset.statusPageId;

        htmx.ajax('PATCH', `/status-pages/${statusPageId}/rename-group/${itemIndex}`, {
            target: '#dashboard-items',
            swap: 'innerHTML',
            values: { name: newName },
        }).catch(() => revertToSpan());
    };

    input.addEventListener('blur', saveRename);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            input.blur();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            revertToSpan();
        }
    });
}

function initializeGroupActions() {
    const addGroupBtn = document.getElementById('add-group-btn');
    const addFilterGroupBtn = document.getElementById('add-filter-group-btn');

    if (addGroupBtn) {
        addGroupBtn.addEventListener('click', () => addGroup('container'));
    }
    if (addFilterGroupBtn) {
        addFilterGroupBtn.addEventListener('click', () => addGroup('filter'));
    }

    initializeFilterInputs();
}

/**
 * Wire change/input handlers on dynamic-group filter inputs. Selects fire
 * on change; tags/search are debounced (500ms) so we don't spam the server
 * on every keystroke.
 */
function initializeFilterInputs() {
    document.body.addEventListener('change', (e) => {
        if (e.target.classList.contains('filter-agent-select') ||
            e.target.classList.contains('filter-type-select')) {
            const itemIndex = parseInt(e.target.dataset.itemIndex);
            updateGroupFilters(itemIndex);
        }
    });

    let inputTimeout;
    document.body.addEventListener('input', (e) => {
        if (e.target.classList.contains('filter-tags-input') ||
            e.target.classList.contains('filter-search-input')) {
            clearTimeout(inputTimeout);
            const itemIndex = parseInt(e.target.dataset.itemIndex);
            inputTimeout = setTimeout(() => updateGroupFilters(itemIndex), 500);
        }
    });
}

function updateGroupFilters(itemIndex) {
    const dashboardItems = document.getElementById('dashboard-items');
    const statusPageId = dashboardItems.dataset.statusPageId;

    const agentSelect = document.querySelector(`.filter-agent-select[data-item-index="${itemIndex}"]`);
    const typeSelect = document.querySelector(`.filter-type-select[data-item-index="${itemIndex}"]`);
    const tagsInput = document.querySelector(`.filter-tags-input[data-item-index="${itemIndex}"]`);
    const searchInput = document.querySelector(`.filter-search-input[data-item-index="${itemIndex}"]`);

    const filterConfig = {};
    if (agentSelect && agentSelect.value) filterConfig.agent_id = agentSelect.value;
    if (typeSelect && typeSelect.value) filterConfig.check_type = typeSelect.value;
    if (tagsInput && tagsInput.value.trim()) {
        filterConfig.tags = tagsInput.value.split(',').map(t => t.trim()).filter(t => t);
    }
    if (searchInput && searchInput.value.trim()) filterConfig.search = searchInput.value.trim();

    htmx.ajax('PATCH', `/status-pages/${statusPageId}/update-group-filters/${itemIndex}`, {
        target: '#dashboard-items',
        swap: 'innerHTML',
        values: { filter_json: JSON.stringify(filterConfig) },
    });
}

function addGroup(type) {
    const dashboardItems = document.getElementById('dashboard-items');
    const statusPageId = dashboardItems.dataset.statusPageId;

    const values = {
        name: `New ${type === 'container' ? 'Container' : 'Dynamic'} Group`,
    };
    // Filter-based groups send an empty filter; container groups
    // omit the field entirely so the server creates a checks-array group.
    if (type === 'filter') {
        values.filter_json = '{}';
    }

    htmx.ajax('POST', `/status-pages/${statusPageId}/add-group`, {
        target: '#dashboard-items',
        swap: 'innerHTML',
        values,
    }).then(() => {
        // Auto-trigger inline rename on the newly added group.
        const allItems = dashboardItems.querySelectorAll('.dashboard-item');
        const newItemIndex = allItems.length - 1;
        setTimeout(() => renameGroup(newItemIndex), 100);
    });
}

function updateItemsCount() {
    const dashboardItems = document.getElementById('dashboard-items');
    if (!dashboardItems) return;
    const items = dashboardItems.querySelectorAll('.dashboard-item');
    const countEl = document.getElementById('items-count');
    if (countEl) {
        countEl.textContent = items.length > 0 ? `${items.length} items` : 'Add checks from the right';
    }
}

function updateGroupSort(groupIndex, sortBy, sortDirection) {
    const dashboardItems = document.getElementById('dashboard-items');
    const statusPageId = dashboardItems.dataset.statusPageId;

    htmx.ajax('PATCH', `/status-pages/${statusPageId}/group/${groupIndex}/sort`, {
        target: '#dashboard-items',
        swap: 'innerHTML',
        values: { sort_by: sortBy, sort_direction: sortDirection },
    });
}

export function init() {
    initializeSortable();
    initializeTagFiltering();
    initializeCheckActions();
    initializeGroupActions();

    document.addEventListener('click', (e) => {
        const target = e.target.closest('button[data-group-sort]');
        if (!target) return;
        const groupIndex = parseInt(target.dataset.groupIndex);
        const sortBy = target.dataset.sortBy;
        const sortDirection = target.dataset.sortDirection;
        updateGroupSort(groupIndex, sortBy, sortDirection);
    });

    // Re-bind drag handles + counts whenever HTMX swaps in fresh dashboard
    // content. Uses the same mechanism for partial swaps and full reloads.
    document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id === 'dashboard-items') {
            initializeSortable();
            updateItemsCount();
        }
    });
}
