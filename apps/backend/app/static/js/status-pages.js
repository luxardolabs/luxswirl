/**
 * Status Pages Module (ES6 Module)
 */

/**
 * Auto-generate slug from name when creating new status page
 */
function setupSlugAutoGeneration() {
    const nameInput = document.getElementById('name');
    const slugInput = document.getElementById('slug');

    if (!nameInput || !slugInput) return;

    // Check if this is a new page (slug should be empty initially)
    const isNewPage = !slugInput.value;

    if (isNewPage) {
        nameInput.addEventListener('input', function(e) {
            // Only auto-fill if slug is empty
            if (!slugInput.value) {
                const slug = e.target.value
                    .toLowerCase()
                    .replace(/[^a-z0-9\s\-_]/g, '')
                    .replace(/\s+/g, '-')
                    .replace(/-+/g, '-')
                    .substring(0, 50);
                slugInput.value = slug;
            }
        });
    }
}

/**
 * Initialize status pages module
 */
export function init() {
    // Listen for HTMX afterSwap to initialize when status page form loads
    document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id === 'side-panel') {
            const form = event.detail.target.querySelector('form[action*="status-pages"]');
            if (form) {
                setupSlugAutoGeneration();
            }
        }
    });

    // Initialize if form already exists on page load
    setupSlugAutoGeneration();
}
