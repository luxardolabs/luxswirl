/**
 * Entry point for the admin UI's ES6 modules.
 *
 * base.html loads this one file with <script type="module" src>; every page
 * module registers its own delegated listeners in init(), so the set below is
 * the whole first-party JS surface for the authenticated layout.
 */

import { init as initApp } from '/static/js/app.js';
import { init as initCheckDetail } from '/static/js/check-detail.js';
import { init as initAlertsForm } from '/static/js/alerts-form.js';
import { init as initCheckForm } from '/static/js/check-form.js';
import { init as initCheckAssignmentSelector } from '/static/js/check-assignment-selector.js';
import { init as initAgentDetail } from '/static/js/agent-detail.js';
import { init as initDatabaseHealth } from '/static/js/database-health.js';
import { init as initSettings } from '/static/js/settings.js';
import { init as initRegistrationKeys } from '/static/js/registration-keys.js';
import { init as initAgents } from '/static/js/agents.js';
import { init as initNotifications } from '/static/js/notifications.js';
import { init as initImportExport } from '/static/js/import-export.js';
import { init as initJobs } from '/static/js/jobs.js';
import { init as initStatusPages } from '/static/js/status-pages.js';
import { init as initDependentsManager } from '/static/js/dependents-manager.js';
import { init as initNetworkScanDetail } from '/static/js/network-scan-detail.js';

function initAll() {
    initApp();
    initCheckDetail();
    initAlertsForm();
    initCheckForm();
    initCheckAssignmentSelector();
    initAgentDetail();
    initDatabaseHealth();
    initSettings();
    initRegistrationKeys();
    initAgents();
    initNotifications();
    initImportExport();
    initJobs();
    initStatusPages();
    initDependentsManager();
    initNetworkScanDetail();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
} else {
    initAll();
}
