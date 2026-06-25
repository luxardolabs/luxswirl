/**
 * Alerts Form - Dynamic form handling for alert creation/editing (ES6 Module)
 */

// Get alert defaults from global config (if available)
const getAlertDefaults = () => {
    return {
        consecutive_failures: 3,
        latency_threshold_ms: 1000
    };
};

/**
 * Toggle check selector visibility based on is_global radio selection
 */
function toggleCheckSelection(show) {
    const checkSelector = document.getElementById('check-selector');
    if (checkSelector) {
        checkSelector.style.display = show ? 'block' : 'none';
    }
}

/**
 * Update trigger config fields based on trigger type selection
 */
function updateTriggerConfig() {
    const triggerSelect = document.getElementById('trigger-type-select');
    const configFields = document.getElementById('trigger-config-fields');

    if (!triggerSelect || !configFields) return;

    const triggerType = triggerSelect.value;
    const defaults = getAlertDefaults();

    if (triggerType === 'status_change') {
        configFields.innerHTML = `
            <div class="space-y-4">
                <div>
                    <label class="label">Consecutive Failures <span class="text-red-400">*</span></label>
                    <input
                        type="number"
                        name="trigger_consecutive_failures"
                        class="input"
                        value="${defaults.consecutive_failures}"
                        min="1"
                        max="100"
                        required
                    >
                    <p class="text-xs text-dark-text-muted mt-1">Number of consecutive failures before triggering alert</p>
                </div>
            </div>
        `;
    } else if (triggerType === 'threshold') {
        configFields.innerHTML = `
            <div>
                <input type="hidden" name="trigger_threshold_metric" value="latency_ms">
                <label class="label mb-3">Alert when response time is: <span class="text-red-400">*</span></label>
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="text-xs text-dark-text-muted block mb-1">Operator</label>
                        <select name="trigger_threshold_operator" class="input" required>
                            <option value=">" selected>Greater than (&gt;)</option>
                            <option value=">=">Greater than or equal (&gt;=)</option>
                            <option value="<">Less than (&lt;)</option>
                            <option value="<=">Less than or equal (&lt;=)</option>
                            <option value="==">Equal to (==)</option>
                        </select>
                    </div>
                    <div>
                        <label class="text-xs text-dark-text-muted block mb-1">Threshold (milliseconds)</label>
                        <input
                            type="number"
                            name="trigger_threshold_value"
                            class="input"
                            value="${defaults.latency_threshold_ms}"
                            min="1"
                            required
                            placeholder="e.g., 1000"
                        >
                    </div>
                </div>
                <p class="text-xs text-dark-text-muted mt-2">Example: "&gt; 1000" alerts when response time exceeds 1 second</p>
            </div>
        `;
    } else if (triggerType === 'ssl_cert_expiry') {
        configFields.innerHTML = `
            <div>
                <label class="label">Alert When Certificate Expires In <span class="text-red-400">*</span></label>
                <p class="text-xs text-dark-text-muted mb-2">Select thresholds (use "Resend Interval" below to control frequency)</p>
                <div class="grid grid-cols-3 gap-2">
                    <label class="flex items-center gap-2 p-2 hover:bg-dark-bg-tertiary rounded cursor-pointer">
                        <input type="checkbox" name="trigger_days_threshold_7" value="7"
                               class="w-4 h-4 text-brand-500 bg-dark-bg-primary border-dark-border rounded">
                        <span class="text-sm">7 days</span>
                    </label>
                    <label class="flex items-center gap-2 p-2 hover:bg-dark-bg-tertiary rounded cursor-pointer">
                        <input type="checkbox" name="trigger_days_threshold_14" value="14"
                               class="w-4 h-4 text-brand-500 bg-dark-bg-primary border-dark-border rounded">
                        <span class="text-sm">14 days</span>
                    </label>
                    <label class="flex items-center gap-2 p-2 hover:bg-dark-bg-tertiary rounded cursor-pointer">
                        <input type="checkbox" name="trigger_days_threshold_21" value="21"
                               class="w-4 h-4 text-brand-500 bg-dark-bg-primary border-dark-border rounded">
                        <span class="text-sm">21 days</span>
                    </label>
                    <label class="flex items-center gap-2 p-2 hover:bg-dark-bg-tertiary rounded cursor-pointer">
                        <input type="checkbox" name="trigger_days_threshold_30" value="30" checked
                               class="w-4 h-4 text-brand-500 bg-dark-bg-primary border-dark-border rounded">
                        <span class="text-sm">30 days</span>
                    </label>
                    <label class="flex items-center gap-2 p-2 hover:bg-dark-bg-tertiary rounded cursor-pointer">
                        <input type="checkbox" name="trigger_days_threshold_60" value="60"
                               class="w-4 h-4 text-brand-500 bg-dark-bg-primary border-dark-border rounded">
                        <span class="text-sm">60 days</span>
                    </label>
                    <label class="flex items-center gap-2 p-2 hover:bg-dark-bg-tertiary rounded cursor-pointer">
                        <input type="checkbox" name="trigger_days_threshold_90" value="90"
                               class="w-4 h-4 text-brand-500 bg-dark-bg-primary border-dark-border rounded">
                        <span class="text-sm">90 days</span>
                    </label>
                </div>
            </div>
        `;
    } else {
        configFields.innerHTML = '';
    }
}

/**
 * Initialize alerts form module
 */
export function init() {
    // Event delegation for trigger type changes
    document.addEventListener('change', (e) => {
        const target = e.target;

        // Handle trigger type dropdown
        if (target.matches('#trigger-type-select')) {
            updateTriggerConfig();
        }

        // Handle is_global radio buttons
        if (target.matches('input[name="is_global"]')) {
            const isSpecific = target.value === 'false';
            toggleCheckSelection(isSpecific);
        }
    });

    // Listen for HTMX afterSwap to initialize forms when loaded into side panel
    document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id === 'side-panel') {
            // Check if alert form was loaded
            const alertForm = document.getElementById('alert-form');
            if (alertForm) {
                // Initialize check selector state based on current is_global selection
                const specificRadio = document.querySelector('input[name="is_global"][value="false"]');
                if (specificRadio && specificRadio.checked) {
                    toggleCheckSelection(true);
                }
            }
        }
    });
}
