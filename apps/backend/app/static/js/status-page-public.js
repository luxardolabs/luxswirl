/**
 * Public status page content behaviour.
 *
 * The content region polls itself; the fade-in animation belongs to first paint
 * only, so it is stripped from cards that arrive in a refresh swap.
 */

export function init() {
    document.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id !== 'status-content') return;
        event.detail.target.querySelectorAll('.animate-fade-in').forEach((card) => {
            card.classList.remove('animate-fade-in');
            card.classList.add('opacity-100');
        });
    });
}

init();
