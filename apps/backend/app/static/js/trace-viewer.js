/**
 * Playwright trace viewer hand-off.
 *
 * trace.playwright.dev cannot be handed a file programmatically, so the flow is:
 * open it in a new tab, then show the panel telling the user to drag the trace
 * file in (with the download link right there). Config comes from the page's
 * data-* attributes.
 */

function showOpenedPanel() {
    document.getElementById('container')?.setAttribute('hidden', '');
    document.getElementById('trace-opened-panel')?.removeAttribute('hidden');
}

function openTraceViewer(btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="inline-block animate-spin mr-2">⏳</span> Loading trace…';

    const opened = window.open('https://trace.playwright.dev', '_blank');
    if (!opened) {
        // Pop-up blocked — leave the download button as the way through.
        btn.disabled = false;
        btn.innerHTML = '❌ Pop-up blocked — use Download Trace instead';
        return;
    }

    btn.innerHTML = '✓ Opened in new tab';
    btn.classList.remove('btn-primary');
    btn.classList.add('bg-green-600');
    setTimeout(showOpenedPanel, 1000);
}

function copyCommand(config) {
    const command = `playwright show-trace ${config.dataset.filename}`;
    navigator.clipboard.writeText(command).then(() => {
        const msg = document.getElementById('copied-message');
        if (!msg) return;
        msg.classList.remove('hidden');
        setTimeout(() => msg.classList.add('hidden'), 3000);
    });
}

export function init() {
    const config = document.getElementById('trace-config');
    if (!config) return;

    document.addEventListener('click', (event) => {
        const opener = event.target.closest('[data-open-trace-viewer]');
        if (opener) {
            openTraceViewer(opener);
            return;
        }
        if (event.target.closest('[data-copy-trace-command]')) {
            copyCommand(config);
        }
    });
}

init();
