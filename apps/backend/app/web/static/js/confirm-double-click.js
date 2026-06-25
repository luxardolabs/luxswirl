/**
 * Double-click-to-confirm for destructive buttons.
 *
 * Any element with [data-confirm-action] is armed on first click (visual
 * swap to a "click again" prompt + 2s timer) and fires its underlying
 * handler (HTMX, form submit, link nav) on the second click within the
 * window. After 2s, it reverts.
 *
 * Capture phase + stopImmediatePropagation prevents HTMX from seeing the
 * first click. Second click is allowed through unchanged.
 *
 * Attributes:
 *   data-confirm-action          marker (any truthy value)
 *   data-confirm-label="..."     optional armed-state label (default "Click again to confirm")
 *   data-confirm-window="2000"   optional ms before reverting (default 2000)
 */

const DEFAULT_LABEL = 'Click again to confirm';
const DEFAULT_WINDOW_MS = 2000;
const ARMED_CLASSES = ['confirm-armed', 'ring-2', 'ring-amber-400', 'ring-offset-2', 'ring-offset-dark-bg-secondary'];

function disarm(btn) {
    if (!btn._confirmOrigHtml) return;
    btn.innerHTML = btn._confirmOrigHtml;
    btn.removeAttribute('data-confirm-armed');
    ARMED_CLASSES.forEach(c => btn.classList.remove(c));
    if (btn._confirmTimer) {
        clearTimeout(btn._confirmTimer);
        btn._confirmTimer = null;
    }
    btn._confirmOrigHtml = null;
}

function arm(btn) {
    const label = btn.dataset.confirmLabel || DEFAULT_LABEL;
    const window_ms = parseInt(btn.dataset.confirmWindow || DEFAULT_WINDOW_MS, 10);
    btn._confirmOrigHtml = btn.innerHTML;
    btn.dataset.confirmArmed = '1';
    ARMED_CLASSES.forEach(c => btn.classList.add(c));
    btn.innerHTML = `
        <svg class="w-4 h-4 mr-1.5 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
        </svg>${label}`;
    btn._confirmTimer = setTimeout(() => disarm(btn), window_ms);
}

export function init() {
    document.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-confirm-action]');
        if (!btn) return;
        if (btn.dataset.confirmArmed === '1') {
            // Second click within window — let it through. Disarm BEFORE
            // releasing so the HTMX handler sees the original markup.
            disarm(btn);
            return;
        }
        // First click — stop everything (including HTMX), arm the button.
        e.preventDefault();
        e.stopImmediatePropagation();
        arm(btn);
    }, true /* capture so we run before HTMX's bubble listener */);
}
