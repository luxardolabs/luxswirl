/**
 * Public status page behaviour.
 *
 * The page polls itself with HTMX; after each swap the "last updated" stamp is
 * reset so a visitor can see the poll landed.
 */

export function init() {
    document.body.addEventListener('htmx:afterSwap', () => {
        const el = document.querySelector('#last-updated span');
        if (el) {
            el.textContent = 'just now';
        }
    });
}

init();
