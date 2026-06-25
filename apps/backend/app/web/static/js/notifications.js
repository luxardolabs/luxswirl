/**
 * Notifications Module (ES6 Module)
 */

/**
 * Build config JSON from form fields before HTMX submission
 */
function setupProviderFormHandler(form) {
    if (!form) return;

    form.addEventListener('htmx:configRequest', function(evt) {
        const formData = new FormData(this);
        const config = {};

        // Collect all config_* fields into JSON
        for (let [key, value] of formData.entries()) {
            if (key.startsWith('config_')) {
                const fieldName = key.replace('config_', '');

                // Parse JSON fields
                if (value && value.trim().startsWith('{')) {
                    try {
                        config[fieldName] = JSON.parse(value);
                    } catch (e) {
                        config[fieldName] = value;
                    }
                }
                // Convert numbers
                else if (!isNaN(value) && value !== '') {
                    config[fieldName] = parseInt(value);
                }
                // Boolean checkboxes
                else if (value === 'true') {
                    config[fieldName] = true;
                }
                // Everything else as string
                else {
                    config[fieldName] = value;
                }
            }
        }

        // Add config_json to the request parameters
        evt.detail.parameters.config_json = JSON.stringify(config);
    });
}

/**
 * Initialize notifications module
 */
export function init() {
    // Listen for HTMX afterSwap to initialize provider form when side panel loads
    document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id === 'side-panel') {
            const providerForm = event.detail.target.querySelector('#provider-form');
            if (providerForm) {
                setupProviderFormHandler(providerForm);
            }
        }
    });

    // Initialize if form already exists on page load
    const existingForm = document.getElementById('provider-form');
    if (existingForm) {
        setupProviderFormHandler(existingForm);
    }
}
