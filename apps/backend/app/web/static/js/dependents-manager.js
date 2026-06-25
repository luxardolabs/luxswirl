/**
 * Dependents manager panel — live selection counter and select-all/clear actions.
 * Event-delegated so it survives HTMX swaps without re-init.
 */

const PANEL = '#dependents-panel';
const LIST = '#dependents-list';
const CHECKBOX = `${LIST} input[type=checkbox][name=dependent_ids]`;
const COUNTER = '#dependents-selected-count';

function updateCount() {
    const counter = document.querySelector(COUNTER);
    if (!counter) return;
    const total = document.querySelectorAll(CHECKBOX).length;
    const checked = document.querySelectorAll(`${CHECKBOX}:checked`).length;
    counter.textContent = `${checked} of ${total} selected`;
}

function setAll(checked) {
    document.querySelectorAll(CHECKBOX).forEach(cb => { cb.checked = checked; });
    updateCount();
}

export function init() {
    document.addEventListener('change', (e) => {
        if (e.target.matches(CHECKBOX)) {
            updateCount();
        }
    });

    document.addEventListener('click', (e) => {
        const btn = e.target.closest('button');
        if (!btn) return;
        if (btn.hasAttribute('data-dep-select-all')) {
            e.preventDefault();
            setAll(true);
        } else if (btn.hasAttribute('data-dep-clear')) {
            e.preventDefault();
            setAll(false);
        }
    });

    document.body.addEventListener('htmx:afterSwap', (e) => {
        if (e.target.querySelector?.(PANEL) || e.target.matches?.(PANEL)) {
            updateCount();
        }
    });
}
