/**
 * Discovered-hosts table on a network-scan job detail panel.
 *
 * The panel arrives via an HTMX swap, so this module registers delegated
 * listeners once and (re)initialises the table whenever a swap brings one in —
 * a per-partial <script> would not re-run for a module that has already
 * evaluated.
 */

const FILTER_ACTIVE = {
    all: 'bg-brand-600/20 text-brand-300 border-brand-600/40',
    new: 'bg-green-600/20 text-green-300 border-green-600/40',
    monitored: 'bg-blue-600/20 text-blue-300 border-blue-600/40',
};
const FILTER_INACTIVE =
    'bg-dark-bg-tertiary text-dark-text-muted border-dark-border hover:border-dark-border-hover';
const SORT_ACTIVE =
    'host-sort-btn p-1 text-[10px] btn-secondary btn-sm !bg-brand-600/20 !text-brand-300 !border-brand-600/40';
const SORT_INACTIVE = 'host-sort-btn p-1 text-[10px] btn-secondary btn-sm';

// Sort state is per-card and survives re-renders of the same panel.
const sortState = { field: 'ip', direction: 'asc' };

function card() {
    return document.getElementById('discovered-hosts-card');
}

function applyFilter(root, filter) {
    let visible = 0;
    root.querySelectorAll('.host-row').forEach((row) => {
        const show = filter === 'all' || row.getAttribute('data-host-status') === filter;
        row.classList.toggle('hidden', !show);
        if (show) visible++;
    });

    const empty = document.getElementById('host-filter-empty');
    if (empty) empty.classList.toggle('hidden', visible > 0);

    root.querySelectorAll('.host-filter-pill').forEach((pill) => {
        const f = pill.getAttribute('data-host-filter');
        pill.className =
            'host-filter-pill px-2.5 py-1 text-xs rounded-full border transition-colors ' +
            (f === filter ? FILTER_ACTIVE[f] : FILTER_INACTIVE);
    });
}

function compareIP(a, b) {
    const pa = a.getAttribute('data-ip').split('.').map(Number);
    const pb = b.getAttribute('data-ip').split('.').map(Number);
    for (let i = 0; i < 4; i++) {
        if (pa[i] !== pb[i]) return pa[i] - pb[i];
    }
    return 0;
}

function compareHostname(a, b) {
    const ha = (a.getAttribute('data-hostname') || a.getAttribute('data-ip')).toLowerCase();
    const hb = (b.getAttribute('data-hostname') || b.getAttribute('data-ip')).toLowerCase();
    return ha.localeCompare(hb);
}

function applySort(root, field, direction) {
    const list = document.getElementById('discovered-hosts-list');
    if (!list) return;

    const compare = field === 'ip' ? compareIP : compareHostname;
    Array.from(root.querySelectorAll('.host-row'))
        .sort((a, b) => (direction === 'asc' ? compare(a, b) : compare(b, a)))
        .forEach((row) => list.appendChild(row));

    root.querySelectorAll('.host-sort-btn').forEach((btn) => {
        const isActive = btn.getAttribute('data-host-sort') === field;
        btn.className = isActive ? SORT_ACTIVE : SORT_INACTIVE;
        const arrow = btn.querySelector('.sort-arrow');
        if (!arrow) return;
        arrow.classList.toggle('hidden', !isActive);
        arrow.classList.toggle('rotate-180', isActive && direction === 'asc');
    });
}

function initTable(root) {
    // Land on "new" when the scan found anything new, else show everything.
    const hasNew = root.querySelector('.host-row[data-host-status="new"]') !== null;
    applyFilter(root, hasNew ? 'new' : 'all');
    sortState.field = 'ip';
    sortState.direction = 'asc';
    applySort(root, sortState.field, sortState.direction);
}

export function init() {
    document.addEventListener('click', (event) => {
        const root = card();
        if (!root) return;

        const pill = event.target.closest('.host-filter-pill');
        if (pill && root.contains(pill)) {
            applyFilter(root, pill.getAttribute('data-host-filter'));
            return;
        }

        const sortBtn = event.target.closest('.host-sort-btn');
        if (sortBtn && root.contains(sortBtn)) {
            const field = sortBtn.getAttribute('data-host-sort');
            if (field === sortState.field) {
                sortState.direction = sortState.direction === 'asc' ? 'desc' : 'asc';
            } else {
                sortState.field = field;
                sortState.direction = 'asc';
            }
            applySort(root, sortState.field, sortState.direction);
        }
    });

    document.body.addEventListener('htmx:afterSwap', () => {
        const root = card();
        if (root) initTable(root);
    });

    const root = card();
    if (root) initTable(root);
}
