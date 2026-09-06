/**
 * Check Form - Dynamic form handling for check creation/editing (ES6 Module)
 */

// Store tags in module scope
let checkFormTags = [];

/**
 * Generate a check name from the check type and target
 */
function generateCheckName() {
    const checkType = document.getElementById('check-type-select');
    const target = document.getElementById('target-input');
    const nameInput = document.getElementById('check-name-input');

    if (!checkType || !target || !nameInput) return;

    if (!target.value) {
        window.showToast?.('Please enter a target first', 'error');
        return;
    }

    // Clean up target to make a valid name
    let cleanTarget = target.value;

    // Extract hostname/IP from different formats
    if (checkType.value === 'http' || checkType.value === 'json') {
        // Extract domain from URL
        try {
            const url = new URL(target.value);
            cleanTarget = url.hostname.replace(/^www\./, '');
        } catch (e) {
            cleanTarget = target.value;
        }
    } else if (checkType.value === 'tcp') {
        // Extract hostname from hostname:port
        cleanTarget = target.value.split(':')[0];
    }

    // Clean up the name: lowercase, replace dots/dashes/special chars with underscores
    cleanTarget = cleanTarget
        .toLowerCase()
        .replace(/[^a-z0-9]/g, '_')
        .replace(/_+/g, '_')
        .replace(/^_|_$/g, '');

    // Generate name: checktype_target
    const generatedName = `${checkType.value}_${cleanTarget}`;

    nameInput.value = generatedName;
}

/**
 * Update form fields based on selected check type
 */
function updateFormFields() {
    const checkType = document.getElementById('check-type-select')?.value;
    if (!checkType) return;

    const httpFields = document.getElementById('http-fields');
    const jsonFields = document.getElementById('json-fields');
    const dnsFields = document.getElementById('dns-fields');
    const mysqlFields = document.getElementById('mysql-fields');
    const postgresFields = document.getElementById('postgres-fields');
    const syntheticFields = document.getElementById('synthetic-fields');
    const bulkFields = document.getElementById('bulk-fields');
    const targetField = document.getElementById('target-field');
    const displayNameField = document.getElementById('display-name-field');
    const verifySslField = document.getElementById('verify-ssl-field');
    const targetInput = document.getElementById('target-input');
    const displayNameInput = document.getElementById('check-name-input');
    const targetHint = document.getElementById('target-hint');

    // Hide all type-specific fields
    if (httpFields) httpFields.classList.add('hidden');
    if (jsonFields) jsonFields.classList.add('hidden');
    if (dnsFields) dnsFields.classList.add('hidden');
    if (mysqlFields) mysqlFields.classList.add('hidden');
    if (postgresFields) postgresFields.classList.add('hidden');
    if (syntheticFields) syntheticFields.classList.add('hidden');
    if (bulkFields) bulkFields.classList.add('hidden');
    if (verifySslField) verifySslField.classList.add('hidden');

    // Show target and display name fields by default, and make them required
    if (targetField && targetInput) {
        targetField.classList.remove('hidden');
        targetInput.required = true;
    }
    if (displayNameField && displayNameInput) {
        displayNameField.classList.remove('hidden');
        displayNameInput.required = true;
    }

    // Update placeholder and hint based on type
    switch(checkType) {
        case 'ping':
            if (targetInput) targetInput.placeholder = '192.168.1.1 or hostname.example.com';
            if (targetHint) targetHint.textContent = 'IP address or hostname to ping';
            break;
        case 'http':
            if (targetInput) targetInput.placeholder = 'https://example.com/api/health';
            if (targetHint) targetHint.textContent = 'Full URL including protocol (http:// or https://)';
            if (httpFields) httpFields.classList.remove('hidden');
            if (verifySslField) verifySslField.classList.remove('hidden');
            break;
        case 'http-bulk':
            // Hide target and display name fields, remove required attribute
            if (targetField && targetInput) {
                targetField.classList.add('hidden');
                targetInput.required = false;
            }
            if (displayNameField && displayNameInput) {
                displayNameField.classList.add('hidden');
                displayNameInput.required = false;
            }
            if (bulkFields) bulkFields.classList.remove('hidden');
            if (httpFields) httpFields.classList.remove('hidden');
            if (verifySslField) verifySslField.classList.remove('hidden');
            break;
        case 'tcp':
            if (targetInput) targetInput.placeholder = 'hostname.example.com:3306';
            if (targetHint) targetHint.textContent = 'Hostname or IP with port (e.g., db.example.com:5432)';
            break;
        case 'dns':
            if (targetInput) targetInput.placeholder = 'example.com';
            if (targetHint) targetHint.textContent = 'Domain name to query';
            if (dnsFields) dnsFields.classList.remove('hidden');
            break;
        case 'json':
            if (targetInput) targetInput.placeholder = 'https://api.example.com/status';
            if (targetHint) targetHint.textContent = 'API endpoint that returns JSON';
            if (httpFields) httpFields.classList.remove('hidden');
            if (jsonFields) jsonFields.classList.remove('hidden');
            if (verifySslField) verifySslField.classList.remove('hidden');
            break;
        case 'mysql':
            if (targetInput) targetInput.placeholder = 'mysql://username:password@host:3306/database';
            if (targetHint) targetHint.textContent = 'MySQL connection string (or use Connection String field below)';
            if (mysqlFields) mysqlFields.classList.remove('hidden');
            break;
        case 'postgres':
            if (targetInput) targetInput.placeholder = 'postgres://username:password@host:5432/database';
            if (targetHint) targetHint.textContent = 'PostgreSQL connection string (or use Connection String field below)';
            if (postgresFields) postgresFields.classList.remove('hidden');
            break;
        case 'synthetic':
            if (targetInput) targetInput.placeholder = 'https://www.example.com';
            if (targetHint) targetHint.textContent = 'Base URL for synthetic check (used in script)';
            if (syntheticFields) syntheticFields.classList.remove('hidden');
            break;
    }
}

/**
 * Toggle advanced settings section
 */
function toggleAdvancedSettings() {
    const content = document.getElementById('advanced-settings-content');
    const toggleText = document.getElementById('advanced-settings-toggle-text');
    const toggleIcon = document.getElementById('advanced-settings-toggle-icon');

    if (!content || !toggleText || !toggleIcon) return;

    if (content.classList.contains('hidden')) {
        content.classList.remove('hidden');
        toggleText.textContent = 'Hide';
        toggleIcon.style.transform = 'rotate(180deg)';
    } else {
        content.classList.add('hidden');
        toggleText.textContent = 'Show';
        toggleIcon.style.transform = 'rotate(0deg)';
    }
}

/**
 * Initialize tags from existing check data
 */
function initTags(existingTags = []) {
    checkFormTags = Array.isArray(existingTags) ? [...existingTags] : [];
    renderTags();
}

/**
 * Render tags in the container
 */
function renderTags() {
    const container = document.getElementById('tags-container');
    if (!container) return;

    container.innerHTML = '';
    checkFormTags.forEach(tag => {
        const pill = document.createElement('div');
        pill.className = 'inline-flex items-center gap-1.5 px-3 py-1 bg-green-600/20 text-green-400 rounded-full text-sm font-medium border border-green-600/50';

        const span = document.createElement('span');
        span.textContent = tag;

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'hover:text-green-300 transition-colors';
        button.dataset.removeTag = tag;
        button.innerHTML = `
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
            </svg>
        `;

        pill.appendChild(span);
        pill.appendChild(button);
        container.appendChild(pill);
    });

    // Update hidden input
    const hiddenInput = document.getElementById('tags-hidden');
    if (hiddenInput) {
        hiddenInput.value = checkFormTags.join(',');
    }
}

/**
 * Add a tag
 */
function addTag() {
    const input = document.getElementById('tag-input');
    if (!input) return;

    const tag = input.value.trim();

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
    input.value = '';
    input.focus();
}

/**
 * Remove a tag
 */
function removeTag(tag) {
    checkFormTags = checkFormTags.filter(t => t !== tag);
    renderTags();
}

/**
 * Handle tag input keydown
 */
function handleTagInput(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        addTag();
    }
}

/**
 * Add tag from browser
 */
function addTagFromBrowser(tag) {
    if (checkFormTags.includes(tag)) {
        return; // Tag already added
    }
    checkFormTags.push(tag);
    renderTags();
}

/**
 * Toggle check tag browser visibility
 */
function toggleCheckTagBrowser() {
    const browser = document.getElementById('check-tag-browser');
    if (browser) {
        browser.classList.toggle('hidden');
    }
}

/**
 * Initialize check form module
 */
export function init() {
    // Event delegation for all check form interactions
    document.addEventListener('click', (e) => {
        const target = e.target.closest('button, [data-generate-check-name], [data-toggle-advanced-settings], [data-add-tag], [data-toggle-tag-browser], [data-remove-tag], [data-add-tag-from-browser]');
        if (!target) return;

        // Generate check name
        if (target.hasAttribute('data-generate-check-name')) {
            e.preventDefault();
            generateCheckName();
        }

        // Toggle advanced settings
        if (target.hasAttribute('data-toggle-advanced-settings')) {
            e.preventDefault();
            toggleAdvancedSettings();
        }

        // Add tag
        if (target.hasAttribute('data-add-tag')) {
            e.preventDefault();
            addTag();
        }

        // Toggle tag browser
        if (target.hasAttribute('data-toggle-tag-browser')) {
            e.preventDefault();
            toggleCheckTagBrowser();
        }

        // Remove tag
        if (target.hasAttribute('data-remove-tag')) {
            e.preventDefault();
            removeTag(target.dataset.removeTag);
        }

        // Add tag from browser
        if (target.hasAttribute('data-add-tag-from-browser')) {
            e.preventDefault();
            addTagFromBrowser(target.dataset.addTagFromBrowser);
        }
    });

    // Event delegation for check type change
    document.addEventListener('change', (e) => {
        if (e.target.matches('#check-type-select')) {
            updateFormFields();
        }
    });

    // Event delegation for tag input keydown
    document.addEventListener('keydown', (e) => {
        if (e.target.matches('#tag-input')) {
            handleTagInput(e);
        }
    });

    // Listen for HTMX afterSwap to initialize form when loaded into side panel
    document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id === 'side-panel') {
            const checkForm = event.detail.target.querySelector('form');
            if (checkForm) {
                // Initialize form fields based on check type
                updateFormFields();

                // Initialize tags from data attribute or empty
                const tagsData = checkForm.dataset.existingTags;
                const existingTags = tagsData ? JSON.parse(tagsData) : [];
                initTags(existingTags);
            }
        }
    });
}
