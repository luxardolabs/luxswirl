/**
 * Agents page — pure client-side UI state only. Reject/delete confirmations
 * use HTMX `hx-confirm` (browser dialog); the click-twice CSS state machine
 * was removed. Server roundtrips happen through the form-wrapper +
 * hx-post/hx-delete pattern in the templates.
 */

import { showToast } from './app.js';

/**
 * Handle pending count updates from backend HX-Trigger events
 */
function initializePendingCountUpdates() {
    document.body.addEventListener('updatePendingCount', function(e) {
        const count = e.detail.count;
        const countEl = document.getElementById('pending-count');

        if (countEl) {
            countEl.textContent = count;
        }

        // If no more pending agents, remove the entire pending section
        if (count === 0) {
            const pendingSection = document.getElementById('pending-section');
            if (pendingSection) {
                pendingSection.remove();
            }
        }
    });
}

/**
 * Agent Tag Management
 */
let agentTags = [];
let tagsContainer = null;
let tagInput = null;
let hiddenInput = null;

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function renderAgentTags() {
    if (!tagsContainer) return;
    tagsContainer.innerHTML = '';
    agentTags.forEach(tag => {
        const pill = document.createElement('div');
        pill.className = 'inline-flex items-center gap-1 px-2 py-0.5 bg-blue-600/20 text-blue-400 rounded text-xs font-mono font-medium border border-blue-600/50';
        pill.innerHTML = `
            <span>${escapeHtml(tag)}</span>
            <button type="button" data-remove-agent-tag="${escapeHtml(tag)}" class="hover:text-blue-300 transition-colors">
                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                </svg>
            </button>
        `;
        tagsContainer.appendChild(pill);
    });

    // Update hidden input (comma-separated)
    if (hiddenInput) {
        hiddenInput.value = agentTags.join(',');
    }
}

function addAgentTag() {
    if (!tagInput) return;
    const tag = tagInput.value.trim();
    if (!tag) return;

    if (agentTags.includes(tag)) {
        showToast('Tag already added', 'error');
        return;
    }

    agentTags.push(tag);
    renderAgentTags();
    tagInput.value = '';
    tagInput.focus();
}

function addAgentTagFromBrowser(tag) {
    if (agentTags.includes(tag)) return;
    agentTags.push(tag);
    renderAgentTags();
    if (tagInput) {
        tagInput.value = '';
        tagInput.focus();
    }
}

function removeAgentTag(tag) {
    agentTags = agentTags.filter(t => t !== tag);
    renderAgentTags();
}

function toggleAgentTagBrowser() {
    const browser = document.getElementById('agent-tag-browser');
    if (browser) {
        browser.classList.toggle('hidden');
    }
}

function initializeAgentTags() {
    // Listen for HTMX afterSwap to initialize tags when agent edit panel loads
    document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id === 'side-panel') {
            const form = event.detail.target.querySelector('#agent-edit-form');
            if (form) {
                // Initialize tag management elements
                tagsContainer = document.getElementById('agent-tags-container');
                tagInput = document.getElementById('agent-tag-input');
                hiddenInput = document.getElementById('agent-tags-hidden');

                // Initialize tags from hidden input
                if (hiddenInput && hiddenInput.value) {
                    agentTags = hiddenInput.value.split(',').map(t => t.trim()).filter(t => t);
                } else {
                    agentTags = [];
                }

                // Render initial tags
                renderAgentTags();

                // Add Enter key handler to tag input
                if (tagInput) {
                    tagInput.addEventListener('keydown', (e) => {
                        if (e.key === 'Enter') {
                            e.preventDefault();
                            addAgentTag();
                        }
                    });
                }
            }

            // Handle successful form submission
            const resultEl = document.getElementById('agent-edit-result');
            if (resultEl) {
                // Set up observer to watch for success messages
                const observer = new MutationObserver(() => {
                    if (resultEl.querySelector('.bg-green-500\\/10')) {
                        // Refresh the agents list after 1 second
                        setTimeout(() => {
                            htmx.trigger('#main-content', 'refresh');
                        }, 1000);
                    }
                });
                observer.observe(resultEl, { childList: true, subtree: true });
            }
        }
    });

    // Event delegation for tag actions
    document.addEventListener('click', (e) => {
        const target = e.target.closest('button');
        if (!target) return;

        // Toggle tag browser
        if (target.hasAttribute('data-toggle-agent-tags')) {
            e.preventDefault();
            toggleAgentTagBrowser();
        }

        // Add tag from browser
        if (target.hasAttribute('data-add-agent-tag')) {
            e.preventDefault();
            const tag = target.getAttribute('data-add-agent-tag');
            if (tag) {
                addAgentTagFromBrowser(tag);
            }
        }

        // Remove tag
        if (target.hasAttribute('data-remove-agent-tag')) {
            e.preventDefault();
            const tag = target.getAttribute('data-remove-agent-tag');
            if (tag) {
                removeAgentTag(tag);
            }
        }
    });
}

/**
 * Initialize agents module
 */
export function init() {
    initializePendingCountUpdates();
    initializeAgentTags();

    // Event delegation for accordion toggles
    document.addEventListener('click', (e) => {
        const target = e.target.closest('button[data-accordion-toggle]');
        if (!target) return;

        const content = target.nextElementSibling;
        const icon = target.querySelector('svg');

        if (content) {
            content.classList.toggle('hidden');
        }
        if (icon) {
            icon.classList.toggle('rotate-180');
        }
    });
}
