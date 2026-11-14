/**
 * Tag picker for the network scan's bulk actions
 * Usage: jobs.js calls window.initBulkTagPicker() once the scan form is swapped in
 */

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
            tagsContainer.appendChild(tagPill(tag, {
                pillClass: 'inline-flex items-center gap-1.5 px-2 py-0.5 bg-green-600/10 text-green-300 rounded text-xs font-mono border border-green-600/30',
                buttonClass: 'hover:text-green-200 transition-colors bulk-tag-remove',
                dataKey: 'tag',
            }));
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

// jobs.html loads this file as a classic script, so it cannot import tag-pill.js.
// A tag is set as text and as a data attribute and never parsed as HTML.
function tagPill(tag, { pillClass, buttonClass, dataKey }) {
    const pill = document.createElement('div');
    pill.className = pillClass;

    const label = document.createElement('span');
    label.textContent = tag;

    const button = document.createElement('button');
    button.type = 'button';
    button.className = buttonClass;
    button.dataset[dataKey] = tag;
    button.innerHTML = `
        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
        </svg>
    `;

    pill.append(label, button);
    return pill;
}

// jobs.js opens the bulk tag picker when htmx swaps the network-scan form in.
window.initBulkTagPicker = initBulkTagPicker;
