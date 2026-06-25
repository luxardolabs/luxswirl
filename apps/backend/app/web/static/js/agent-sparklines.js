/**
 * Agent sparkline interactivity (ES6 Module)
 * Handles hover effects for SVG sparkline dots
 */

export function init() {
    // Event delegation for sparkline hover zones
    document.addEventListener('mouseenter', (e) => {
        // Safety check: ensure target is an element with classList
        if (e.target && e.target.classList && e.target.classList.contains('sparkline-hover-zone')) {
            // Find the corresponding dot (next sibling)
            const dot = e.target.nextElementSibling;
            if (dot && dot.hasAttribute('data-dot')) {
                dot.setAttribute('r', '4');
            }
        }
    }, true);

    document.addEventListener('mouseleave', (e) => {
        // Safety check: ensure target is an element with classList
        if (e.target && e.target.classList && e.target.classList.contains('sparkline-hover-zone')) {
            // Find the corresponding dot (next sibling)
            const dot = e.target.nextElementSibling;
            if (dot && dot.hasAttribute('data-dot')) {
                dot.setAttribute('r', '0');
            }
        }
    }, true);
}
