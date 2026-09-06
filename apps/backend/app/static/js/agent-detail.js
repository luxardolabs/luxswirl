/**
 * Agent detail page chart functionality (ES6 Module)
 */

/**
 * Get common chart options
 */
function getChartOptions(yAxisLabel, minY = undefined, maxY = undefined) {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
            mode: 'index',
            intersect: false,
        },
        plugins: {
            legend: {
                display: true,
                labels: {
                    color: '#94a3b8',
                    font: {
                        size: 11
                    }
                }
            },
            tooltip: {
                backgroundColor: 'rgba(15, 23, 42, 0.9)',
                titleColor: '#f1f5f9',
                bodyColor: '#cbd5e1',
                borderColor: 'rgba(148, 163, 184, 0.2)',
                borderWidth: 1,
                padding: 10,
                displayColors: true,
            }
        },
        scales: {
            y: {
                beginAtZero: true,
                min: minY,
                max: maxY,
                title: {
                    display: true,
                    text: yAxisLabel,
                    color: '#94a3b8',
                    font: {
                        size: 11
                    }
                },
                grid: {
                    color: 'rgba(255, 255, 255, 0.05)'
                },
                ticks: {
                    color: '#94a3b8',
                    font: {
                        size: 10
                    }
                }
            },
            x: {
                type: 'time',
                time: {
                    displayFormats: {
                        hour: 'HH:mm',
                        minute: 'HH:mm'
                    }
                },
                grid: {
                    color: 'rgba(255, 255, 255, 0.05)'
                },
                ticks: {
                    color: '#94a3b8',
                    maxRotation: 0,
                    autoSkipPadding: 20,
                    font: {
                        size: 10
                    }
                }
            }
        }
    };
}

/**
 * Initialize CPU Usage Chart
 */
function initCPUChart(timestamps, cpuData) {
    const chartEl = document.getElementById('cpu-chart');
    if (!chartEl) return;

    new Chart(chartEl, {
        type: 'line',
        data: {
            labels: timestamps,
            datasets: [{
                label: 'CPU %',
                data: cpuData,
                borderColor: 'rgba(59, 130, 246, 1)',  // blue-500
                backgroundColor: 'rgba(59, 130, 246, 0.1)',
                tension: 0.4,
                fill: true,
                pointRadius: 2,
                pointHoverRadius: 4,
            }]
        },
        options: getChartOptions('CPU Usage (%)', 0, 100)
    });
}

/**
 * Initialize Memory Usage Chart
 */
function initMemoryChart(timestamps, memoryData) {
    const chartEl = document.getElementById('memory-chart');
    if (!chartEl) return;

    new Chart(chartEl, {
        type: 'line',
        data: {
            labels: timestamps,
            datasets: [{
                label: 'Memory (MB)',
                data: memoryData,
                borderColor: 'rgba(168, 85, 247, 1)',  // purple-500
                backgroundColor: 'rgba(168, 85, 247, 0.1)',
                tension: 0.4,
                fill: true,
                pointRadius: 2,
                pointHoverRadius: 4,
            }]
        },
        options: getChartOptions('Memory Usage (MB)', 0)
    });
}

/**
 * Initialize Queue Depth Chart
 */
function initQueueChart(timestamps, queueData) {
    const chartEl = document.getElementById('queue-chart');
    if (!chartEl) return;

    new Chart(chartEl, {
        type: 'line',
        data: {
            labels: timestamps,
            datasets: [{
                label: 'Queue Depth',
                data: queueData,
                borderColor: 'rgba(245, 158, 11, 1)',  // amber-500
                backgroundColor: 'rgba(245, 158, 11, 0.2)',
                tension: 0.4,
                fill: true,
                pointRadius: 2,
                pointHoverRadius: 4,
            }]
        },
        options: getChartOptions('Queue Depth', 0)
    });
}

/**
 * Initialize Checks Executed/Succeeded Chart
 */
function initChecksChart(timestamps, executedData, succeededData) {
    const chartEl = document.getElementById('checks-chart');
    if (!chartEl) return;

    new Chart(chartEl, {
        type: 'line',
        data: {
            labels: timestamps,
            datasets: [
                {
                    label: 'Executed',
                    data: executedData,
                    borderColor: 'rgba(100, 116, 139, 1)',  // slate-500
                    backgroundColor: 'rgba(100, 116, 139, 0.1)',
                    tension: 0.4,
                    fill: false,
                    pointRadius: 2,
                    pointHoverRadius: 4,
                },
                {
                    label: 'Succeeded',
                    data: succeededData,
                    borderColor: 'rgba(16, 185, 129, 1)',  // emerald-500
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    tension: 0.4,
                    fill: true,
                    pointRadius: 2,
                    pointHoverRadius: 4,
                }
            ]
        },
        options: getChartOptions('Checks Count', 0)
    });
}

/**
 * Initialize File Descriptors Chart (SWIRL-57)
 */
function initFDChart(timestamps, fdData) {
    const chartEl = document.getElementById('fd-chart');
    if (!chartEl) return;

    new Chart(chartEl, {
        type: 'line',
        data: {
            labels: timestamps,
            datasets: [{
                label: 'Open FDs',
                data: fdData,
                borderColor: 'rgba(234, 179, 8, 1)',  // yellow-500
                backgroundColor: 'rgba(234, 179, 8, 0.1)',
                tension: 0.4,
                fill: true,
                pointRadius: 2,
                pointHoverRadius: 4,
            }]
        },
        options: getChartOptions('File Descriptors', 0)
    });
}

/**
 * Initialize Subprocesses Chart (SWIRL-57)
 */
function initSubprocessChart(timestamps, subprocessData) {
    const chartEl = document.getElementById('subprocess-chart');
    if (!chartEl) return;

    new Chart(chartEl, {
        type: 'line',
        data: {
            labels: timestamps,
            datasets: [{
                label: 'Subprocesses',
                data: subprocessData,
                borderColor: 'rgba(239, 68, 68, 1)',  // red-500
                backgroundColor: 'rgba(239, 68, 68, 0.1)',
                tension: 0.4,
                fill: true,
                pointRadius: 2,
                pointHoverRadius: 4,
            }]
        },
        options: getChartOptions('Subprocess Count', 0)
    });
}

/**
 * Initialize agent metrics charts
 */
function initAgentMetricsCharts() {
    const dataEl = document.getElementById('agent-metrics-data');
    if (!dataEl) {
        return;
    }

    try {
        const metricsData = JSON.parse(dataEl.textContent);
        const timestamps = metricsData.timestamps.map(t => new Date(t));

        // Create CPU chart
        initCPUChart(timestamps, metricsData.cpu_data);

        // Create Memory chart
        initMemoryChart(timestamps, metricsData.memory_data);

        // Create Queue Depth chart
        initQueueChart(timestamps, metricsData.queue_data);

        // Create Checks Executed/Succeeded chart
        initChecksChart(timestamps, metricsData.checks_executed, metricsData.checks_succeeded);

        // SWIRL-57: Create File Descriptors chart
        if (metricsData.fd_data) {
            initFDChart(timestamps, metricsData.fd_data);
        }

        // SWIRL-57: Create Subprocesses chart
        if (metricsData.subprocess_data) {
            initSubprocessChart(timestamps, metricsData.subprocess_data);
        }
    } catch (e) {
        console.error('Failed to initialize agent metrics charts:', e);
    }
}

/**
 * Initialize agent detail module
 */
export function init() {
    // Initialize charts immediately if data is available
    initAgentMetricsCharts();
}
