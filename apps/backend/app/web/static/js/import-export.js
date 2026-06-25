/**
 * Import/Export Module (ES6 Module)
 */

function displayFileName(name) {
    const fileNameEl = document.getElementById('file-name');
    if (fileNameEl) {
        fileNameEl.textContent = `Selected: ${name}`;
        fileNameEl.classList.remove('hidden');
    }
}

function handleDrop(event, dropZone) {
    event.preventDefault();
    dropZone.classList.remove('border-brand-500', 'bg-dark-bg-tertiary/50');

    const files = event.dataTransfer.files;
    if (files.length > 0) {
        const fileInput = document.getElementById('file-input');
        if (fileInput) {
            fileInput.files = files;
            displayFileName(files[0].name);
        }
    }
}

function handleFileSelect(event) {
    const files = event.target.files;
    if (files.length > 0) {
        displayFileName(files[0].name);
    }
}

function setupDropZone(dropZone) {
    if (!dropZone) return;

    // Drag over
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('border-brand-500', 'bg-dark-bg-tertiary/50');
    });

    // Drag leave
    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('border-brand-500', 'bg-dark-bg-tertiary/50');
    });

    // Drop
    dropZone.addEventListener('drop', (e) => {
        handleDrop(e, dropZone);
    });

    // Click to open file browser
    dropZone.addEventListener('click', () => {
        const fileInput = document.getElementById('file-input');
        if (fileInput) {
            fileInput.click();
        }
    });
}

function setupFileInput(fileInput) {
    if (!fileInput) return;

    fileInput.addEventListener('change', (e) => {
        handleFileSelect(e);
    });
}

/**
 * Initialize import/export module
 */
export function init() {
    // Listen for HTMX afterSwap to initialize when import/export panel loads
    document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id === 'side-panel') {
            const importForm = event.detail.target.querySelector('#import-form');
            if (importForm) {
                const dropZone = document.getElementById('drop-zone');
                const fileInput = document.getElementById('file-input');

                setupDropZone(dropZone);
                setupFileInput(fileInput);
            }
        }

        // Listen for successful import to refresh checks table
        if (event.detail.target.id === 'import-result') {
            const resultEl = event.detail.target;
            if (resultEl.querySelector('.bg-green-500\\/10')) {
                // Refresh the checks list after 2 seconds
                setTimeout(() => {
                    htmx.trigger('#checks-table', 'refresh');
                }, 2000);
            }
        }
    });

    // Initialize if panel already exists on page load
    const existingDropZone = document.getElementById('drop-zone');
    const existingFileInput = document.getElementById('file-input');
    if (existingDropZone && existingFileInput) {
        setupDropZone(existingDropZone);
        setupFileInput(existingFileInput);
    }
}
