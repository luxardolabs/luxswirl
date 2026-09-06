/**
 * LuxSwirl — Main Application JavaScript (ES6 Module)
 *
 * Core handlers: sidebar toggle, toast system, panel close bridge, refresh
 * page trigger, HTMX error display.
 *
 * Slide-over panels are driven by Alpine.js — see macros/panels.html. This
 * file only owns:
 *   - The bridge that translates `data-close-panel` clicks AND the
 *     `closeSidePanel` HX-Trigger event into the standard Alpine close
 *     sequence (set show=false, wait for the leave transition, clear the
 *     container).
 *   - Toast notifications.
 *   - The `refreshPage` HX-Trigger reload.
 *   - HTMX error display.
 */

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const mainContent = document.getElementById('main-content');

    sidebar.classList.toggle('sidebar-collapsed');
    sidebar.classList.toggle('sidebar-expanded');

    if (sidebar.classList.contains('sidebar-collapsed')) {
        mainContent.style.marginLeft = '4rem';
        mainContent.style.width = 'calc(100% - 4rem)';
    } else {
        mainContent.style.marginLeft = '16rem';
        mainContent.style.width = 'calc(100% - 16rem)';
    }
}

/**
 * Trigger the Alpine close sequence on whatever panel is currently mounted
 * inside #panel-container. Mirrors the inline expression on the macro
 * (`show = false; setTimeout(...innerHTML='', 300)`).
 */
function closePanel() {
    const container = document.getElementById('panel-container');
    if (!container) return;
    const wrapper = container.querySelector('[x-data]');
    if (wrapper && window.Alpine) {
        const data = window.Alpine.$data(wrapper);
        if (data && 'show' in data) {
            data.show = false;
            setTimeout(() => { container.innerHTML = ''; }, 300);
            return;
        }
    }
    container.innerHTML = '';
}

function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.id = 'toast-' + Date.now();

    const bgColor = type === 'success' ? 'bg-green-500/10 border-green-600/30' : 'bg-red-900/20 border-red-700';
    const textColor = type === 'success' ? 'text-green-400' : 'text-red-400';
    const icon = type === 'success'
        ? '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>'
        : '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>';

    toast.className = 'p-4 ' + bgColor + ' border rounded-lg shadow-lg min-w-[300px] max-w-md opacity-0 transition-opacity duration-300';
    toast.innerHTML = `
        <div class="flex items-center gap-3">
            <svg class="w-5 h-5 ${textColor} flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                ${icon}
            </svg>
            <p class="text-sm ${textColor} flex-1">${message}</p>
        </div>
    `;

    container.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '1'; }, 10);
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, type === 'error' ? 5000 : 3000);
}

export function init() {
    window.showToast = showToast;
    window.closePanel = closePanel;

    document.addEventListener('click', (e) => {
        // Sidebar toggle
        const toggleBtn = e.target.closest('button[data-toggle-sidebar]');
        if (toggleBtn) {
            e.preventDefault();
            toggleSidebar();
            return;
        }

        // Close-panel bridge — keeps existing data-close-panel call sites working
        const closeBtn = e.target.closest('[data-close-panel]');
        if (closeBtn) {
            e.preventDefault();
            closePanel();
            return;
        }

        // Dismiss inline error card
        const dismissBtn = e.target.closest('button[data-dismiss-error]');
        if (dismissBtn) {
            e.preventDefault();
            dismissBtn.closest('.card')?.remove();
        }
    });

    document.body.addEventListener('showToast', (event) => {
        showToast(event.detail.message, event.detail.type);
    });

    // Backend-triggered close (HX-Trigger: closeSidePanel) — Alpine-aware close
    document.body.addEventListener('closeSidePanel', () => {
        closePanel();
    });

    document.body.addEventListener('refreshPage', () => {
        window.location.reload();
    });

    // Maintenance jobs (LUXSWIRL-105): when a backend cascading mutation
    // finishes, reload the page so the user sees a clean post-operation state.
    // Fires from partials/maintenance/job_status.html on terminal 'done'.
    window.addEventListener('maintenanceJobDone', () => {
        // Brief delay so the user sees the "Done." flash before reload.
        setTimeout(() => window.location.reload(), 600);
    });

    // Allow 4xx HTML responses through (so form validation HTML still swaps)
    document.body.addEventListener('htmx:beforeSwap', (event) => {
        if (event.detail.xhr.status >= 400 && event.detail.xhr.status < 600) {
            const contentType = event.detail.xhr.getResponseHeader('content-type');
            if (contentType && contentType.includes('text/html')) {
                event.detail.shouldSwap = true;
                event.detail.isError = false;
            }
        }
    });

    document.body.addEventListener('htmx:responseError', (event) => {
        const contentType = event.detail.xhr.getResponseHeader('content-type');
        if (!contentType || !contentType.includes('text/html')) {
            showToast('An error occurred. Please try again.', 'error');
        }
    });
}

export { toggleSidebar, closePanel, showToast };
