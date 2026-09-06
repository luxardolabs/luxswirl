/**
 * Registration Keys page — pure client-side UI state only (clipboard copy
 * with success/failure feedback). All server roundtrips happen via HTMX
 * forms in the templates.
 */

/**
 * Copy the text content of the element identified by `data-copy-source` on
 * the clicked button to the clipboard. Shows a transient "Copied!" badge
 * on the button on success; toasts a friendly error on failure.
 */
function copyToClipboard(button) {
    const sourceId = button.dataset.copySource;
    if (!sourceId) return;
    const sourceEl = document.getElementById(sourceId);
    if (!sourceEl) return;

    navigator.clipboard.writeText(sourceEl.innerText).then(() => {
        const originalHTML = button.innerHTML;
        button.innerHTML = `
            <svg class="w-4 h-4 inline mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
            </svg>
            Copied!
        `;
        button.classList.add('bg-green-600', 'hover:bg-green-700');

        setTimeout(() => {
            button.innerHTML = originalHTML;
            button.classList.remove('bg-green-600', 'hover:bg-green-700');
        }, 2000);
    }).catch(() => {
        if (window.showToast) {
            window.showToast(
                'Failed to copy to clipboard. Please select and copy manually.',
                'error'
            );
        }
    });
}

export function init() {
    document.addEventListener('click', (e) => {
        const target = e.target.closest('button[data-copy-source]');
        if (target) {
            e.preventDefault();
            copyToClipboard(target);
        }
    });
}
