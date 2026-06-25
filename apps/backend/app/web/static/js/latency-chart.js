/**
 * Latency chart interactivity (ES6 Module)
 * Handles hover tooltips and animations for SVG latency charts
 */

export function init() {
    let currentHoverZone = null;
    let hideTimeout = null;

    // Event delegation for chart hover zones - use mouseover/mouseout which bubble properly
    document.addEventListener('mouseover', (e) => {
        if (!e.target.classList.contains('latency-chart-hover-zone')) return;

        // Cancel any pending hide
        if (hideTimeout) {
            clearTimeout(hideTimeout);
            hideTimeout = null;
        }

        // Prevent rapid re-triggering on same element
        if (currentHoverZone === e.target) return;
        currentHoverZone = e.target;

        // Show corresponding dot
        const dot = e.target.nextElementSibling;
        if (dot && dot.hasAttribute('data-chart-dot')) {
            dot.setAttribute('r', '6');
        }

        // Get data from hover zone
        const latency = e.target.dataset.latency;
        const timestamp = e.target.dataset.timestamp;
        const success = e.target.dataset.success === 'true';

        // Find tooltip element
        const svg = e.target.closest('svg');
        const tooltip = svg?.querySelector('#latency-tooltip');
        if (!tooltip) return;

        // Update tooltip content
        const timeEl = tooltip.querySelector('#latency-tooltip-time');
        const latencyEl = tooltip.querySelector('#latency-tooltip-latency');
        const statusEl = tooltip.querySelector('#latency-tooltip-status');

        if (timeEl && latencyEl && statusEl) {
            const date = new Date(timestamp);
            timeEl.textContent = date.toLocaleTimeString();
            latencyEl.textContent = `${parseFloat(latency).toFixed(1)} ms`;
            statusEl.textContent = success ? 'Success' : 'Failed';
            statusEl.setAttribute('fill', success ? '#10b981' : '#ef4444');
        }

        // Position tooltip near cursor with smart positioning
        const cx = parseFloat(e.target.dataset.pointX);
        const cy = parseFloat(e.target.dataset.pointY);

        // Smart positioning - keep tooltip in bounds (160px wide, 80px tall)
        let tooltipX = cx - 80; // Center horizontally on point
        let tooltipY = cy < 100 ? cy + 15 : cy - 95; // Above or below based on Y

        // Keep within bounds
        tooltipX = Math.max(10, Math.min(tooltipX, 630)); // 800 - 160 - 10 = 630
        tooltipY = Math.max(5, Math.min(tooltipY, 115)); // 200 - 80 - 5 = 115

        tooltip.setAttribute('transform', `translate(${tooltipX}, ${tooltipY})`);
        tooltip.style.display = 'block';
        tooltip.style.opacity = '1';
    });

    document.addEventListener('mouseout', (e) => {
        if (!e.target.classList.contains('latency-chart-hover-zone')) return;

        // Always hide the dot when leaving a zone
        const dot = e.target.nextElementSibling;
        if (dot && dot.hasAttribute('data-chart-dot')) {
            dot.setAttribute('r', '0');
        }

        // Don't hide tooltip if we're moving to another hover zone
        if (e.relatedTarget && e.relatedTarget.classList?.contains('latency-chart-hover-zone')) {
            return;
        }

        currentHoverZone = null;

        // Delay hiding tooltip slightly to prevent flashing during rapid movements
        const svg = e.target.closest('svg');
        const tooltip = svg?.querySelector('#latency-tooltip');
        if (tooltip) {
            hideTimeout = setTimeout(() => {
                tooltip.style.display = 'none';
                hideTimeout = null;
            }, 50); // 50ms delay
        }
    });
}
