/**
 * Settings - Two-click confirmation for reset actions (ES6 Module)
 */

let pendingReset = null;
let resetTimeout = null;
let resetInProgress = false;

/**
 * Clear pending reset state
 */
function clearPendingReset() {
    if (pendingReset) {
        const btn = document.querySelector(`button[data-setting-key="${pendingReset}"]`);
        if (btn) {
            btn.classList.remove('!bg-orange-600', 'animate-pulse', 'shadow-lg', 'shadow-orange-500/50');
            btn.title = 'Reset to default';
        }
        pendingReset = null;
    }
    if (resetTimeout) {
        clearTimeout(resetTimeout);
        resetTimeout = null;
    }
}

/**
 * Handle reset button clicks with two-click confirmation
 */
function handleResetClick(evt) {
    const resetBtn = evt.target.closest('button[hx-post*="/reset"]');

    if (!resetBtn) return;

    // Extract setting key from hx-post URL
    const postUrl = resetBtn.getAttribute('hx-post');
    if (!postUrl || !postUrl.includes('/settings/')) return;

    const settingKey = postUrl.split('/settings/')[1].split('/reset')[0];
    resetBtn.setAttribute('data-setting-key', settingKey);

    // Prevent HTMX from handling this
    evt.preventDefault();
    evt.stopPropagation();

    // Check if this is the second click
    if (pendingReset === settingKey && !resetInProgress) {
        // Second click - trigger the reset
        resetInProgress = true;

        // Trigger HTMX request manually
        htmx.ajax('POST', postUrl, {
            target: resetBtn.getAttribute('hx-target'),
            swap: resetBtn.getAttribute('hx-swap')
        }).then(() => {
            clearPendingReset();
            resetInProgress = false;
        }).catch(() => {
            clearPendingReset();
            resetInProgress = false;
        });
    } else if (!resetInProgress) {
        // First click - show visual confirmation prompt
        clearPendingReset();
        pendingReset = settingKey;

        // Add pulsing orange state
        resetBtn.classList.add('!bg-orange-600', 'animate-pulse', 'shadow-lg', 'shadow-orange-500/50');
        resetBtn.title = 'Click again to confirm reset';

        // Clear after 3 seconds
        resetTimeout = setTimeout(() => {
            clearPendingReset();
        }, 3000);
    }
}

/**
 * Copy metrics token to clipboard with visual feedback
 */
function copyTokenToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        // Get elements
        const button = document.getElementById('copy-token-btn');
        const confirmation = document.getElementById('copy-confirmation');
        const originalHTML = button.innerHTML;

        // Change button to checkmark and green
        button.innerHTML = '<svg class="w-5 h-5 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>';
        button.classList.remove('btn-secondary');
        button.classList.add('bg-green-600/20', 'border-green-600/30');

        // Show confirmation message
        confirmation.classList.remove('opacity-0');
        confirmation.classList.add('opacity-100');

        // Reset after 2 seconds
        setTimeout(() => {
            button.innerHTML = originalHTML;
            button.classList.add('btn-secondary');
            button.classList.remove('bg-green-600/20', 'border-green-600/30');
            confirmation.classList.remove('opacity-100');
            confirmation.classList.add('opacity-0');
        }, 2000);
    }).catch(err => {
        console.error('Failed to copy:', err);
    });
}

/**
 * Download metrics token as a text file
 */
function downloadToken(token) {
    // Create a blob with the token
    const blob = new Blob([token], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);

    // Create a temporary link and trigger download
    const link = document.createElement('a');
    link.href = url;
    link.download = 'luxswirl-metrics-token.txt';
    document.body.appendChild(link);
    link.click();

    // Clean up
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

/**
 * Initialize settings module
 */
export function init() {
    // Event delegation for all settings interactions
    document.addEventListener('click', (e) => {
        const target = e.target.closest('button');
        if (!target) return;

        // Handle reset button clicks (use capture phase to intercept before HTMX)
        if (target.hasAttribute('hx-post') && target.getAttribute('hx-post').includes('/reset')) {
            handleResetClick(e);
            return;
        }

        // Handle copy token button
        if (target.hasAttribute('data-copy-token')) {
            e.preventDefault();
            const token = target.dataset.copyToken;
            copyTokenToClipboard(token);
        }

        // Handle download token button
        if (target.hasAttribute('data-download-token')) {
            e.preventDefault();
            const token = target.dataset.downloadToken;
            downloadToken(token);
        }
    }, true); // Use capture phase for reset handler
}
