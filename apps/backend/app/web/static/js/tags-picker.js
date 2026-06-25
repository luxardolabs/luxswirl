/**
 * Reusable tag picker for check tags and bulk action tags
 * Usage: Call initTagPicker() with container IDs after DOM loads
 */

// Initialize tag picker for check forms
function initCheckFormTagPicker(existingTags = []) {
    const tagsContainer = document.getElementById('tags-container');
    const tagInput = document.getElementById('tag-input');
    const hiddenInput = document.getElementById('tags-hidden');
    const browser = document.getElementById('check-tag-browser');

    if (!tagsContainer || !tagInput || !hiddenInput) return;

    let checkFormTags = existingTags;

    function renderTags() {
        tagsContainer.innerHTML = '';
        checkFormTags.forEach(tag => {
            const pill = document.createElement('div');
            pill.className = 'inline-flex items-center gap-1.5 px-2 py-0.5 bg-green-600/10 text-green-300 rounded text-xs font-mono border border-green-600/30';
            pill.innerHTML = `
                <span>${escapeHtml(tag)}</span>
                <button type="button" class="hover:text-green-200 transition-colors tag-remove" data-tag="${escapeHtml(tag)}">
                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                    </svg>
                </button>
            `;
            tagsContainer.appendChild(pill);
        });

        // Update hidden input
        hiddenInput.value = checkFormTags.join(',');

        // Add event listeners to remove buttons
        tagsContainer.querySelectorAll('.tag-remove').forEach(btn => {
            btn.addEventListener('click', () => {
                const tag = btn.dataset.tag;
                checkFormTags = checkFormTags.filter(t => t !== tag);
                renderTags();
            });
        });
    }

    function addTag() {
        const tag = tagInput.value.trim();
        if (!tag) return;

        // Validate tag format (alphanumeric, dash, underscore)
        if (!/^[a-zA-Z0-9_-]+$/.test(tag)) {
            window.showToast?.('Tags can only contain letters, numbers, dashes, and underscores', 'error');
            return;
        }

        // Check for duplicates
        if (checkFormTags.includes(tag)) {
            window.showToast?.('Tag already exists', 'error');
            return;
        }

        checkFormTags.push(tag);
        renderTags();
        tagInput.value = '';
        tagInput.focus();
    }

    function addTagFromBrowser(tag) {
        if (checkFormTags.includes(tag)) {
            return; // Tag already added
        }
        checkFormTags.push(tag);
        renderTags();
    }

    function toggleBrowser() {
        if (browser) {
            browser.classList.toggle('hidden');
        }
    }

    // Event listeners
    tagInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            addTag();
        }
    });

    // Expose functions globally for button onclick
    window.addTag = addTag;
    window.addTagFromBrowser = addTagFromBrowser;
    window.toggleCheckTagBrowser = toggleBrowser;

    // Initialize
    renderTags();
}

// Initialize tag picker for bulk actions (network scan)
function initBulkTagPicker() {
    const tagsContainer = document.getElementById('bulk-tags-container');
    const tagInput = document.getElementById('bulk-tag-input');
    const hiddenInput = document.getElementById('bulk-tags-hidden');
    const browser = document.getElementById('bulk-tag-browser');
    const browserBtn = document.getElementById('bulk-tag-browser-btn');

    if (!tagsContainer || !tagInput || !hiddenInput) return;

    // Prevent double initialization
    if (tagsContainer.dataset.initialized === 'true') return;
    tagsContainer.dataset.initialized = 'true';

    let bulkTags = [];

    function renderTags() {
        tagsContainer.innerHTML = '';
        bulkTags.forEach(tag => {
            const pill = document.createElement('div');
            pill.className = 'inline-flex items-center gap-1.5 px-2 py-0.5 bg-green-600/10 text-green-300 rounded text-xs font-mono border border-green-600/30';
            pill.innerHTML = `
                <span>${escapeHtml(tag)}</span>
                <button type="button" class="hover:text-green-200 transition-colors bulk-tag-remove" data-tag="${escapeHtml(tag)}">
                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                    </svg>
                </button>
            `;
            tagsContainer.appendChild(pill);
        });

        // Update hidden input (comma-separated)
        hiddenInput.value = bulkTags.join(',');

        // Add event listeners to remove buttons
        tagsContainer.querySelectorAll('.bulk-tag-remove').forEach(btn => {
            btn.addEventListener('click', () => {
                const tag = btn.dataset.tag;
                bulkTags = bulkTags.filter(t => t !== tag);
                renderTags();
            });
        });
    }

    function addTag() {
        const tag = tagInput.value.trim();
        if (!tag) return;

        if (bulkTags.includes(tag)) {
            window.showToast?.('Tag already added', 'error');
            return;
        }

        bulkTags.push(tag);
        renderTags();
        tagInput.value = '';
        tagInput.focus();
    }

    function addTagFromBrowser(tag) {
        if (bulkTags.includes(tag)) return;
        bulkTags.push(tag);
        renderTags();
        if (tagInput) {
            tagInput.value = '';
            tagInput.focus();
        }
    }

    function toggleBrowser() {
        if (browser) {
            browser.classList.toggle('hidden');
        }
    }

    // Event listeners
    tagInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            addTag();
        }
    });

    // Add button click handler
    const addBtn = document.getElementById('bulk-tag-add-btn');
    if (addBtn) {
        addBtn.addEventListener('click', addTag);
    }

    // Browser button click handler
    if (browserBtn) {
        browserBtn.addEventListener('click', toggleBrowser);
    }

    // Browser tag option click handlers
    document.querySelectorAll('.bulk-tag-browser-option').forEach(btn => {
        btn.addEventListener('click', () => {
            const tag = btn.dataset.tag;
            addTagFromBrowser(tag);
        });
    });

    // Expose functions globally for backward compatibility (check form may still use them)
    window.addBulkTag = addTag;
    window.addBulkTagFromBrowser = addTagFromBrowser;
    window.toggleBulkTagBrowser = toggleBrowser;

    // Initialize
    renderTags();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
