/**
 * Jobs page — pure client-side UI state only.
 * Server roundtrips (job create, check create, bulk actions) all go through
 * HTMX. This module handles only the relative-timestamp formatter and the
 * priority-slider live label, plus reinitializing tag pickers after HTMX
 * swaps load new content.
 */

/**
 * Format timestamps as relative time (e.g., "5m ago"). Pure client-side —
 * runs on page load and after every HTMX swap so freshly-injected rows pick
 * up the formatting.
 */
function updateTimeAgo() {
    document.querySelectorAll('.timeago').forEach(el => {
        const timestamp = new Date(el.dataset.timestamp);
        const now = new Date();
        const seconds = Math.floor((now - timestamp) / 1000);

        let text;
        if (seconds < 60) {
            text = seconds + 's ago';
        } else if (seconds < 3600) {
            text = Math.floor(seconds / 60) + 'm ago';
        } else if (seconds < 86400) {
            text = Math.floor(seconds / 3600) + 'h ago';
        } else {
            text = Math.floor(seconds / 86400) + 'd ago';
        }

        el.textContent = text;
    });
}

/**
 * Setup priority slider for job creation form — pure client-side UI state,
 * mirrors the slider's value into a label as the user drags.
 */
function setupPrioritySlider() {
    const slider = document.querySelector('[data-priority-slider]');
    const valueDisplay = document.getElementById('priority-value');

    if (slider && valueDisplay) {
        slider.addEventListener('input', function() {
            valueDisplay.textContent = this.value;
        });
    }
}

export function init() {
    // Stop click propagation on action-cells so row-level click handlers
    // (e.g. open-detail) don't fire when the user clicks a button inside.
    document.addEventListener('click', (e) => {
        const actionCell = e.target.closest('td[data-stop-propagation]');
        if (actionCell) {
            e.stopPropagation();
        }
    });

    // After every HTMX swap: re-format timestamps for freshly-injected rows
    // and rewire the priority slider / bulk tag picker if the swapped content
    // includes them.
    document.body.addEventListener('htmx:afterSwap', (event) => {
        updateTimeAgo();

        if (event.detail.target.id === 'side-panel') {
            const jobForm = event.detail.target.querySelector('#job-create-form');
            if (jobForm) {
                setupPrioritySlider();
            }
        }

        if (document.getElementById('bulk-tags-container')) {
            if (typeof window.initBulkTagPicker === 'function') {
                window.initBulkTagPicker();
            }
        }
    });

    updateTimeAgo();
    setupPrioritySlider();
}
