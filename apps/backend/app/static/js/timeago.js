/**
 * Relative-time labels for elements carrying data-timestamp.
 *
 * Renders "42s ago" / "7m ago" / "3h ago", falling back to the viewer's local
 * clock time for anything older than a day, and puts the full local timestamp
 * in the title so hovering shows the exact moment.
 */

function render(el) {
    const timestamp = new Date(el.dataset.timestamp);
    const seconds = Math.floor((new Date() - timestamp) / 1000);

    let text;
    if (seconds < 60) {
        text = seconds + 's ago';
    } else if (seconds < 3600) {
        text = Math.floor(seconds / 60) + 'm ago';
    } else if (seconds < 86400) {
        text = Math.floor(seconds / 3600) + 'h ago';
    } else {
        // Older than a day reads better as a wall-clock time than "13d ago".
        text = timestamp.toLocaleTimeString('en-US', {
            hour: 'numeric',
            minute: '2-digit',
            hour12: true
        }).toLowerCase();
    }

    el.textContent = text;
    el.title = timestamp.toLocaleString();
}

export function init() {
    document.querySelectorAll('.timeago').forEach(render);
}

init();
