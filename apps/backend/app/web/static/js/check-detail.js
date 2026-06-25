/**
 * Check detail panel functionality (ES6 Module)
 */

import { init as initLatencyChart } from './latency-chart.js';

// Toggle recent results accordion
function toggleRecentResults() {
    const content = document.getElementById('recent-results-content');
    const icon = document.getElementById('recent-results-icon');

    if (content && icon) {
        content.classList.toggle('hidden');
        icon.classList.toggle('rotate-180');
    }
}

// Toggle artifacts accordion
function toggleArtifacts() {
    const content = document.getElementById('artifacts-content');
    const icon = document.getElementById('artifacts-toggle-icon');

    if (content && icon) {
        if (content.classList.contains('hidden')) {
            content.classList.remove('hidden');
            icon.style.transform = 'rotate(180deg)';
        } else {
            content.classList.add('hidden');
            icon.style.transform = 'rotate(0deg)';
        }
    }
}

// Toggle step timings table (for synthetic checks)
function toggleStepTimings() {
    const table = document.getElementById('step-timings-table');
    const toggle = document.getElementById('step-timings-toggle');

    if (table && toggle) {
        if (table.classList.contains('hidden')) {
            table.classList.remove('hidden');
            toggle.textContent = 'Hide Details ▲';
        } else {
            table.classList.add('hidden');
            toggle.textContent = 'Show Details ▼';
        }
    }
}

// Open image in modal
function openImageModal(src, filename) {
    // Create modal overlay
    const modal = document.createElement('div');
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4';
    modal.onclick = () => modal.remove();

    // Create image container
    const container = document.createElement('div');
    container.className = 'relative max-w-7xl max-h-full';
    container.onclick = (e) => e.stopPropagation();

    // Create close button
    const closeBtn = document.createElement('button');
    closeBtn.className = 'absolute -top-10 right-0 text-white hover:text-brand-400 transition-colors';
    closeBtn.innerHTML = `
        <svg class="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
        </svg>
    `;
    closeBtn.onclick = () => modal.remove();

    // Create image
    const img = document.createElement('img');
    img.src = src;
    img.alt = filename;
    img.className = 'max-w-full max-h-[90vh] rounded-lg shadow-2xl';

    // Create download link
    const downloadBtn = document.createElement('a');
    downloadBtn.href = src;
    downloadBtn.download = filename;
    downloadBtn.className = 'absolute -top-10 left-0 text-white hover:text-brand-400 transition-colors flex items-center gap-2';
    downloadBtn.innerHTML = `
        <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
        </svg>
        <span>Download</span>
    `;

    container.appendChild(closeBtn);
    container.appendChild(downloadBtn);
    container.appendChild(img);
    modal.appendChild(container);
    document.body.appendChild(modal);
}


// Initialize module with event delegation
export function init() {
    // Initialize latency chart interactivity
    initLatencyChart();

    // Event delegation for accordion toggles and image clicks
    document.addEventListener('click', (e) => {
        const target = e.target.closest('button, img');
        if (!target) return;

        // Toggle recent results accordion
        if (target.hasAttribute('data-toggle-recent-results')) {
            e.preventDefault();
            toggleRecentResults();
        }

        // Toggle artifacts accordion
        if (target.hasAttribute('data-toggle-artifacts')) {
            e.preventDefault();
            toggleArtifacts();
        }

        // Open image modal
        if (target.hasAttribute('data-image-modal')) {
            e.preventDefault();
            const src = target.dataset.imageSrc || target.src;
            const filename = target.dataset.imageFilename || target.alt;
            openImageModal(src, filename);
        }
    });

    // Event delegation for check row selection (status-v2)
    document.addEventListener('click', (e) => {
        const checkRow = e.target.closest('.check-row');
        if (!checkRow) return;

        // Remove selection from all rows
        document.querySelectorAll('.check-row').forEach(row => {
            row.classList.remove('bg-dark-bg-secondary');
            row.style.borderLeftColor = 'transparent';
        });

        // Add selection to clicked row
        checkRow.classList.add('bg-dark-bg-secondary');
        checkRow.style.borderLeftColor = '#3b82f6'; // blue-500
    });
}

