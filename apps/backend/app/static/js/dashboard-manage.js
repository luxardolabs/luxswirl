/**
 * Dashboard management — drag-and-drop, inline rename, tag filtering, and
 * inline filter inputs for the status-pages /manage view.
 *
 * Server roundtrips all go through `htmx.ajax(...)` (manageAjax) so HTMX owns the swap
 * lifecycle (hx-confirm dialogs, htmx:afterSwap re-binding, indicators).
 * Pure-client JS lives here only for things that have no server roundtrip:
 * Sortable.js drag visuals, the inline rename input lifecycle, debounced
 * filter inputs that flush to the server, and the available-checks-card
 * "added/not added" visual indicator.
 */

import { tagPill } from './tag-pill.js';

let sortableInstance = null;
let selectedTags = [];
let checkActionsInitialized = false;
// True while initializeSortable tears down the old instance: Sortable's
// destroy() ends a drag in progress and fires its onEnd.
let reinitializingSortable = false;
// Debounced tag/search filter saves not yet sent, one per group, keyed by the
// group's item index: { timer, input }.
const pendingFilterSaves = new Map();
// The open inline rename, if any: { input, isEdited, save }.
let openRename = null;


/**
 * Initialize Sortable.js for drag-and-drop reordering. The drag visuals are
 * pure client-side; the persist call (saveItemOrder) goes through htmx.ajax.
 */
function initializeSortable() {
    const dashboardItems = document.getElementById('dashboard-items');
    if (!dashboardItems) return;

    if (sortableInstance) {
        reinitializingSortable = true;
        try {
            sortableInstance.destroy();
        } finally {
            reinitializingSortable = false;
        }
    }

    sortableInstance = Sortable.create(dashboardItems, {
        animation: 150,
        handle: '.drag-handle',
        ghostClass: 'sortable-ghost',
        chosenClass: 'sortable-chosen',
        dragClass: 'sortable-drag',
        group: 'dashboard',
        onEnd: saveItemOrderIfMoved
    });

    document.querySelectorAll('.group-checks-container').forEach(container => {
        Sortable.create(container, {
            animation: 150,
            handle: '.drag-handle',
            ghostClass: 'sortable-ghost',
            chosenClass: 'sortable-chosen',
            dragClass: 'sortable-drag',
            // A group holds checks, never another group.
            group: {
                name: 'dashboard',
                put: (to, from, dragEl) => dragEl.dataset.itemType !== 'group',
            },
            onEnd: saveItemOrderIfMoved
        });
    });
}

/**
 * Sortable onEnd for the page list and every group's list. Saves the order
 * only when the drop moved the item: a drop into another list (a group or
 * out of one) at any index, or a new index in the same list. A drag that a
 * redraw ended (the old instance destroyed, or the dragged item no longer in
 * the drawn list) saves nothing, since a no-op reorder would still move the
 * page's version and get the user's next queued edit refused.
 */
function saveItemOrderIfMoved(evt) {
    if (reinitializingSortable) return;
    const dashboardItems = document.getElementById('dashboard-items');
    if (!dashboardItems || !dashboardItems.contains(evt.item)) return;
    if (evt.from !== evt.to || evt.oldIndex !== evt.newIndex) {
        saveItemOrder();
    }
}

/**
 * Persist the current item order. Computes the new layout from DOM state,
 * sends it to /reorder as a JSON-encoded form field, and lets HTMX swap
 * the refreshed dashboard_items partial back into #dashboard-items. The
 * htmx:afterSwap listener then re-runs initializeSortable + initializeCheckActions.
 */
function saveItemOrder() {
    const dashboardItems = document.getElementById('dashboard-items');

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

    manageAjax('POST', dashboardItems.dataset.reorderUrl, {
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
            selectedTagsContainer.appendChild(tagPill(tag, {
                pillClass: 'inline-flex items-center gap-1 px-2 py-1 bg-brand-600/20 text-brand-300 rounded-full text-xs border border-brand-500/30',
                buttonClass: 'remove-tag-btn hover:text-white',
                dataKey: 'tag',
                element: 'span',
            }));
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
 * dashboard_items partial; its swap re-syncs the available-checks cards
 * (syncCheckCards). A refused add swaps nothing, so no card changes.
 */
function addCheckToDashboard(checkEl) {
    const dashboardItems = document.getElementById('dashboard-items');

    manageAjax('POST', dashboardItems.dataset.addCheckUrl, {
        values: { check_id: checkEl.dataset.checkId },
    });
}

/**
 * Mark each available-checks card added exactly when the page, as last
 * drawn from the server, names its check. Run after either list swaps, so
 * whichever answer lands last, the cards agree with the page.
 */
function syncCheckCards() {
    const onPage = new Set(
        Array.from(
            document.querySelectorAll('#dashboard-items [data-check-id]'),
            (el) => el.dataset.checkId
        )
    );
    document.querySelectorAll('.available-check').forEach((checkEl) => {
        updateCheckCardVisual(checkEl, onPage.has(checkEl.dataset.checkId));
    });
}

/**
 * The page version the editor list was drawn from (dashboard_items.html).
 */
function drawnPageVersion() {
    return document.getElementById('dashboard-page-version')?.dataset.pageVersion ?? '';
}

/**
 * Send every edit the user has typed but not yet saved: each group's pending
 * tag/search filter save and an open rename. Run before another editor
 * request is issued, so the typed text is sent ahead of it rather than
 * discarded when that request's answer redraws the list.
 */
function flushPendingEdits() {
    const pending = Array.from(pendingFilterSaves);
    pendingFilterSaves.clear();
    pending.forEach(([itemIndex, { timer, input }]) => {
        clearTimeout(timer);
        if (input.isConnected) {
            updateGroupFilters(itemIndex);
        }
    });
    if (openRename) {
        openRename.save();
    }
}

/**
 * Before a response redraws the editor list, tell the user about typed text
 * the redraw is about to discard (a filter not yet sent, an edited rename
 * still open), and forget it: its inputs and the page they name are gone.
 */
function warnDiscardedEdits(event) {
    if (event.detail.target?.id !== 'dashboard-items' || !event.detail.shouldSwap) {
        return;
    }
    let filterDropped = false;
    pendingFilterSaves.forEach(({ timer, input }) => {
        clearTimeout(timer);
        if (input.isConnected) filterDropped = true;
    });
    pendingFilterSaves.clear();
    const renameDropped = Boolean(openRename && openRename.input.isConnected && openRename.isEdited());
    openRename = null;

    const dropped = [];
    if (renameDropped) dropped.push('group name');
    if (filterDropped) dropped.push('group filter');
    if (dropped.length) {
        window.showToast?.(
            `The list was redrawn before your ${dropped.join(' and ')} change was saved. Enter it again.`,
            'error'
        );
    }
}

/**
 * Every manage-page roundtrip. Its request belongs to #dashboard-items, whose
 * hx-sync="this:queue all" sends each in turn, and carries the page version
 * read now, from the same drawn page as any item index the caller read. One
 * queued behind a request that changes the page is refused (409) instead of
 * applied to whatever item now sits at that index, and the editor redraws.
 */
function manageAjax(verb, url, options) {
    flushPendingEdits();
    return htmx.ajax(verb, url, {
        source: document.getElementById('dashboard-items'),
        target: '#dashboard-items',
        swap: 'innerHTML',
        headers: { 'X-Page-Version': drawnPageVersion() },
        ...options,
    });
}

/**
 * Redraw the editor list from the server, after a request was refused as
 * made on an older page.
 */
function redrawEditor() {
    const dashboardItems = document.getElementById('dashboard-items');
    htmx.ajax('GET', dashboardItems.dataset.itemsUrl, {
        source: dashboardItems,
        target: '#dashboard-items',
        swap: 'innerHTML',
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
    // The version of the page this rename was opened on, with this group's
    // index: a swap can blur the input after the new page is drawn.
    const drawnVersion = drawnPageVersion();

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

    // Settled once the rename is sent, reverted or discarded: a later blur
    // or flush sends nothing.
    let settled = false;
    const settle = () => {
        settled = true;
        if (openRename?.input === input) openRename = null;
    };

    const saveRename = () => {
        if (settled) return;
        settle();
        // A redraw removed the input: its group's URL and the page it names
        // are gone with it.
        if (!input.isConnected) return;
        const newName = input.value.trim();
        if (!newName || newName === currentName) {
            revertToSpan();
            return;
        }
        manageAjax('PATCH', groupElement.dataset.renameUrl, {
            values: { name: newName },
            headers: { 'X-Page-Version': drawnVersion },
        }).catch(() => revertToSpan());
    };

    openRename?.save();
    openRename = {
        input,
        isEdited: () => {
            const newName = input.value.trim();
            return Boolean(newName) && newName !== currentName;
        },
        save: saveRename,
    };

    input.addEventListener('blur', saveRename);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            input.blur();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            settle();
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
 * Wire change/input handlers on dynamic-group filter inputs. Selects save on
 * change; tags/search save 500ms after the last keystroke in that group, or
 * at once when the input loses focus (its change event).
 */
function initializeFilterInputs() {
    document.body.addEventListener('change', (e) => {
        if (e.target.classList.contains('filter-agent-select') ||
            e.target.classList.contains('filter-type-select')) {
            const itemIndex = parseInt(e.target.dataset.itemIndex);
            updateGroupFilters(itemIndex);
        }
    });

    const isTextFilter = (el) =>
        el.classList.contains('filter-tags-input') ||
        el.classList.contains('filter-search-input');

    // One timer per group, so typing in another group does not cancel this
    // group's save. The page version is read when the save is sent, from the
    // page that drew the input; an input a redraw removed has no page to save
    // to (warnDiscardedEdits reports it).
    document.body.addEventListener('input', (e) => {
        if (!isTextFilter(e.target)) return;
        const input = e.target;
        const itemIndex = parseInt(input.dataset.itemIndex);
        clearTimeout(pendingFilterSaves.get(itemIndex)?.timer);
        const timer = setTimeout(() => {
            pendingFilterSaves.delete(itemIndex);
            if (input.isConnected) {
                updateGroupFilters(itemIndex);
            }
        }, 500);
        pendingFilterSaves.set(itemIndex, { timer, input });
    });

    // Leaving the input (to click another editor control) sends its save now,
    // ahead of whatever the click sends.
    document.body.addEventListener('change', (e) => {
        if (!isTextFilter(e.target)) return;
        const itemIndex = parseInt(e.target.dataset.itemIndex);
        if (pendingFilterSaves.has(itemIndex) && e.target.isConnected) {
            updateGroupFilters(itemIndex);
        }
    });
}

function updateGroupFilters(itemIndex) {
    const groupElement = document.querySelector(`.dashboard-item[data-item-index="${itemIndex}"]`);
    if (!groupElement) return;

    const agentSelect = document.querySelector(`.filter-agent-select[data-item-index="${itemIndex}"]`);
    const typeSelect = document.querySelector(`.filter-type-select[data-item-index="${itemIndex}"]`);
    const tagsInput = document.querySelector(`.filter-tags-input[data-item-index="${itemIndex}"]`);
    const searchInput = document.querySelector(`.filter-search-input[data-item-index="${itemIndex}"]`);

    // This save sends every field of the group, typed text included.
    clearTimeout(pendingFilterSaves.get(itemIndex)?.timer);
    pendingFilterSaves.delete(itemIndex);

    const filterConfig = {};
    if (agentSelect && agentSelect.value) filterConfig.agent_id = agentSelect.value;
    if (typeSelect && typeSelect.value) filterConfig.check_type = typeSelect.value;
    if (tagsInput && tagsInput.value.trim()) {
        filterConfig.tags = tagsInput.value.split(',').map(t => t.trim()).filter(t => t);
    }
    if (searchInput && searchInput.value.trim()) filterConfig.search = searchInput.value.trim();

    manageAjax('PATCH', groupElement.dataset.filtersUrl, {
        values: { filter_json: JSON.stringify(filterConfig) },
    });
}

function addGroup(type) {
    const dashboardItems = document.getElementById('dashboard-items');

    const values = {
        name: `New ${type === 'container' ? 'Container' : 'Dynamic'} Group`,
    };
    // Filter-based groups send an empty filter; container groups
    // omit the field entirely so the server creates a checks-array group.
    if (type === 'filter') {
        values.filter_json = '{}';
    }

    // Its swap opens the new group's rename (renameAddedGroup).
    manageAjax('POST', dashboardItems.dataset.addGroupUrl, { values });
}

/**
 * Open the inline rename on the group an add-group swap just drew: the last
 * top-level item (a group's own checks carry no data-item-index). Run from
 * that request's own swap, since a queued request's promise resolves before
 * it is sent.
 */
function renameAddedGroup(event) {
    const dashboardItems = document.getElementById('dashboard-items');
    if (event.detail.requestConfig?.path !== dashboardItems.dataset.addGroupUrl) {
        return;
    }
    // A refused add-group still fires afterSwap: app.js swaps error responses
    // (and marks them not errors) so their toasts render.
    if (event.detail.xhr.status >= 300) {
        return;
    }
    const allItems = dashboardItems.querySelectorAll('.dashboard-item[data-item-index]');
    const newItemIndex = allItems.length - 1;
    setTimeout(() => {
        // Not when the user has since put focus somewhere (typing in another
        // field): the rename would take it from them.
        const active = document.activeElement;
        if (active && active !== document.body &&
            !active.closest('#add-group-btn, #add-filter-group-btn')) {
            return;
        }
        renameGroup(newItemIndex);
    }, 100);
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
    const groupElement = document.querySelector(`.dashboard-item[data-item-index="${groupIndex}"]`);
    if (!groupElement) return;

    manageAjax('PATCH', groupElement.dataset.sortUrl, {
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

    // An editor request (manageAjax, or a remove button inside the list) made
    // on an older page is refused with 409: redraw the list from the server.
    const dashboardItems = document.getElementById('dashboard-items');
    dashboardItems.addEventListener('htmx:afterRequest', (event) => {
        if (event.detail.xhr && event.detail.xhr.status === 409) {
            redrawEditor();
        }
    });

    // A redraw of the list discards typed text not yet saved: say so. On
    // document, so it runs after app.js's body listener has decided whether
    // an error response swaps.
    document.addEventListener('htmx:beforeSwap', warnDiscardedEdits);

    // Re-bind drag handles + counts whenever HTMX swaps in fresh dashboard
    // content. Uses the same mechanism for partial swaps and full reloads.
    // Either list swapping re-syncs the available-checks cards with the page.
    document.body.addEventListener('htmx:afterSwap', (event) => {
        const swapped = event.detail.target.id;
        if (swapped === 'dashboard-items') {
            initializeSortable();
            updateItemsCount();
            renameAddedGroup(event);
        }
        if (swapped === 'dashboard-items' || swapped === 'available-checks-list') {
            syncCheckCards();
        }
    });
}

// Loaded with <script type="module" src>, so the module self-initialises.
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
