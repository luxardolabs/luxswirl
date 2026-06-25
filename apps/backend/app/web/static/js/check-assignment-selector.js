/**
 * Check Assignment Selector - Tag-based agent selector for replicate/distribute modes (ES6 Module)
 */

// Module scope variables
let selectorTags = [];
let availableTags = [];

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Render selector tags
 */
function renderTags() {
    const tagsContainer = document.getElementById('selector-tags-container');
    if (!tagsContainer) return;

    tagsContainer.innerHTML = '';
    selectorTags.forEach(tag => {
        const pill = document.createElement('div');
        pill.className = 'inline-flex items-center gap-1.5 px-3 py-1 bg-blue-600/20 text-blue-400 rounded-full text-sm font-medium border border-blue-600/50';

        const span = document.createElement('span');
        span.textContent = tag;

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'hover:text-blue-300 transition-colors';
        button.dataset.removeSelectorTag = tag;
        button.innerHTML = `
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
            </svg>
        `;

        pill.appendChild(span);
        pill.appendChild(button);
        tagsContainer.appendChild(pill);
    });

    updateHiddenField();
    updateBrowserButtons();
}

/**
 * Update browser buttons state based on selected tags
 */
function updateBrowserButtons() {
    const browsers = [
        document.getElementById('tag-browser'),
        document.getElementById('tag-browser-bottom')
    ];

    browsers.forEach(browser => {
        if (!browser) return;

        const buttons = browser.querySelectorAll('[data-add-selector-tag-from-suggestion]');
        buttons.forEach(button => {
            const tag = button.dataset.addSelectorTagFromSuggestion;
            if (selectorTags.includes(tag)) {
                button.classList.add('opacity-50', 'cursor-not-allowed', 'pointer-events-none');
                button.classList.remove('hover:bg-brand-600/20', 'hover:text-brand-400', 'hover:border-brand-600/30');
            } else {
                button.classList.remove('opacity-50', 'cursor-not-allowed', 'pointer-events-none');
                button.classList.add('hover:bg-brand-600/20', 'hover:text-brand-400', 'hover:border-brand-600/30');
            }
        });
    });
}

/**
 * Update hidden field with JSON selector
 */
function updateHiddenField() {
    const hiddenInput = document.getElementById('agent-selector-hidden');
    if (!hiddenInput) return;

    const matchModeSelect = document.getElementById('tag-match-mode');
    const matchMode = matchModeSelect ? matchModeSelect.value : 'all';

    hiddenInput.value = JSON.stringify({
        tags: selectorTags,
        match_mode: matchMode
    });
}

/**
 * Set match mode (all/any)
 */
function setMatchMode(mode) {
    const hiddenInput = document.getElementById('tag-match-mode');
    const allButton = document.getElementById('match-mode-all');
    const anyButton = document.getElementById('match-mode-any');
    const slider = document.getElementById('match-mode-slider');
    const helpText = document.getElementById('match-mode-help');

    if (!hiddenInput || !allButton || !anyButton || !slider) return;

    // Update hidden input
    hiddenInput.value = mode;

    // Update slider position and button colors
    if (mode === 'all') {
        // Move slider to left (ALL position)
        slider.style.left = '0.25rem';
        slider.style.right = 'auto';

        // Update text colors
        allButton.className = 'relative z-10 px-3 py-1.5 text-xs font-medium transition-colors duration-200 text-brand-400';
        anyButton.className = 'relative z-10 px-3 py-1.5 text-xs font-medium transition-colors duration-200 text-dark-text-muted';

        if (helpText) {
            helpText.innerHTML = `
                <strong>ALL tags (AND):</strong> Agent must have ALL selected tags.
                <br>
                Simple tags: <code class="bg-dark-bg-tertiary px-1 py-0.5 rounded">production</code> or
                key:value pairs: <code class="bg-dark-bg-tertiary px-1 py-0.5 rounded">role:monitor</code>
            `;
        }
    } else {
        // Move slider to right (ANY position)
        slider.style.left = 'auto';
        slider.style.right = '0.25rem';

        // Update text colors
        allButton.className = 'relative z-10 px-3 py-1.5 text-xs font-medium transition-colors duration-200 text-dark-text-muted';
        anyButton.className = 'relative z-10 px-3 py-1.5 text-xs font-medium transition-colors duration-200 text-brand-400';

        if (helpText) {
            helpText.innerHTML = `
                <strong>ANY tag (OR):</strong> Agent needs at least ONE of the selected tags.
                <br>
                Simple tags: <code class="bg-dark-bg-tertiary px-1 py-0.5 rounded">production</code> or
                key:value pairs: <code class="bg-dark-bg-tertiary px-1 py-0.5 rounded">role:monitor</code>
            `;
        }
    }

    updateHiddenField();
}

/**
 * Show tag suggestions
 */
function showSuggestions(filter) {
    const suggestionsDiv = document.getElementById('tag-suggestions');
    if (!suggestionsDiv || !availableTags.length) return;

    const filtered = availableTags.filter(tag =>
        tag.toLowerCase().includes(filter.toLowerCase()) &&
        !selectorTags.includes(tag)
    );

    if (filtered.length === 0) {
        suggestionsDiv.classList.add('hidden');
        return;
    }

    suggestionsDiv.innerHTML = filtered.slice(0, 10).map(tag => `
        <div class="px-4 py-2 hover:bg-dark-bg-tertiary cursor-pointer text-sm font-mono text-dark-text-primary transition-colors"
             data-add-selector-tag-from-suggestion="${escapeHtml(tag)}">
            ${escapeHtml(tag)}
        </div>
    `).join('');
    suggestionsDiv.classList.remove('hidden');
}

/**
 * Hide tag suggestions
 */
function hideSuggestions() {
    const suggestionsDiv = document.getElementById('tag-suggestions');
    if (suggestionsDiv) {
        setTimeout(() => suggestionsDiv.classList.add('hidden'), 200);
    }
}

/**
 * Add selector tag from input
 */
function addSelectorTag() {
    const tagInput = document.getElementById('selector-tag-input');
    if (!tagInput) return;

    const tag = tagInput.value.trim();
    if (!tag) return;

    if (selectorTags.includes(tag)) {
        window.showToast?.('Tag already added', 'error');
        return;
    }

    selectorTags.push(tag);
    renderTags();
    tagInput.value = '';
    tagInput.focus();
    hideSuggestions();
}

/**
 * Add selector tag from suggestion/browser
 */
function addSelectorTagFromSuggestion(tag) {
    if (selectorTags.includes(tag)) return;

    selectorTags.push(tag);
    renderTags();

    const tagInput = document.getElementById('selector-tag-input');
    if (tagInput) {
        tagInput.value = '';
        tagInput.focus();
    }
    hideSuggestions();
}

/**
 * Remove selector tag
 */
function removeSelectorTag(tag) {
    selectorTags = selectorTags.filter(t => t !== tag);
    renderTags();
}

/**
 * Toggle tag browser visibility
 */
function toggleTagBrowser() {
    const browser = document.getElementById('tag-browser');
    if (browser) {
        browser.classList.toggle('hidden');
        updateBrowserButtons(); // Update button states when showing browser
    }
}

/**
 * Initialize assignment selector with existing data
 */
function initializeSelector(existingSelector = null, availableTagsList = []) {
    // Reset state
    selectorTags = [];
    availableTags = availableTagsList;

    // Load existing selector tags
    if (existingSelector && existingSelector.tags) {
        selectorTags = existingSelector.tags;

        // Set match mode if provided
        if (existingSelector.match_mode) {
            setMatchMode(existingSelector.match_mode);
        }
    }

    // Setup event listeners for this selector instance
    const tagInput = document.getElementById('selector-tag-input');
    if (tagInput) {
        // Remove old listeners by cloning and replacing
        const newTagInput = tagInput.cloneNode(true);
        tagInput.parentNode.replaceChild(newTagInput, tagInput);

        // Add new listeners
        newTagInput.addEventListener('input', (e) => {
            const value = e.target.value.trim();
            if (value.length > 0) {
                showSuggestions(value);
            } else {
                hideSuggestions();
            }
        });

        newTagInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                addSelectorTag();
            } else if (e.key === 'Escape') {
                hideSuggestions();
            }
        });

        newTagInput.addEventListener('blur', hideSuggestions);
    }

    // Render tags
    renderTags();
}

/**
 * Initialize check assignment selector module
 */
export function init() {
    // Event delegation for selector interactions
    document.addEventListener('click', (e) => {
        const target = e.target.closest('button, div, [data-set-match-mode], [data-toggle-tag-browser], [data-remove-selector-tag], [data-add-selector-tag-from-suggestion]');
        if (!target) return;

        // Set match mode
        if (target.hasAttribute('data-set-match-mode')) {
            e.preventDefault();
            setMatchMode(target.dataset.setMatchMode);
        }

        // Toggle tag browser
        if (target.hasAttribute('data-toggle-tag-browser')) {
            e.preventDefault();
            toggleTagBrowser();
        }

        // Remove selector tag
        if (target.hasAttribute('data-remove-selector-tag')) {
            e.preventDefault();
            removeSelectorTag(target.dataset.removeSelectorTag);
        }

        // Add tag from suggestion/browser
        if (target.hasAttribute('data-add-selector-tag-from-suggestion')) {
            e.preventDefault();
            addSelectorTagFromSuggestion(target.dataset.addSelectorTagFromSuggestion);
        }
    });

    // Listen for HTMX afterSwap to initialize selector when loaded
    document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id === 'assignment-selector-container') {
            // Assignment selector was loaded, check for initialization data
            const container = event.detail.target;
            const selectorDiv = container.querySelector('[data-selector-init]');

            if (selectorDiv) {
                try {
                    const initData = JSON.parse(selectorDiv.dataset.selectorInit || '{}');
                    initializeSelector(initData.existingSelector, initData.availableTags || []);
                } catch (e) {
                    console.error('Failed to parse selector init data:', e);
                    initializeSelector(null, []);
                }
            } else {
                // Initialize with empty state if no data attribute found
                initializeSelector(null, []);
            }
        }
    });
}
