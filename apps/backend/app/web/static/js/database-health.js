/**
 * Database Health - Growth Chart (ES6 Module)
 */

let growthChart = null;

/**
 * Format timestamp for chart labels based on time range
 */
function formatChartLabel(timestamp, hours) {
    const date = new Date(timestamp);

    if (hours <= 24) {
        // Hourly: show time only
        return date.toLocaleTimeString('en-US', {
            hour: '2-digit',
            minute: '2-digit',
            hour12: false
        });
    } else if (hours <= 168) {
        // 7 days: show weekday + date
        return date.toLocaleDateString('en-US', {
            weekday: 'short',
            month: 'short',
            day: 'numeric'
        });
    } else {
        // 30+ days: show date only
        return date.toLocaleDateString('en-US', {
            month: 'short',
            day: 'numeric'
        });
    }
}

/**
 * Format number with thousands separator
 */
function formatNumber(num) {
    return num.toLocaleString('en-US');
}

/**
 * Initialize or update the growth chart
 */
async function updateGrowthChart(hours) {
    const chartEl = document.getElementById('database-growth-chart');
    if (!chartEl) return;

    try {
        // Fetch data from API
        const response = await fetch(`/database-health/chart-data?hours=${hours}`);
        const result = await response.json();

        if (result.error) {
            console.error('Error loading chart data:', result.error);
            return;
        }

        const chartData = result.data || [];

        // Prepare chart data
        const labels = chartData.map(d => formatChartLabel(d.timestamp, hours));
        const resultsData = chartData.map(d => d.results_mb);
        const artifactsData = chartData.map(d => d.artifacts_mb);
        const checksData = chartData.map(d => d.checks_mb);
        const agentMetricsData = chartData.map(d => d.agent_metrics_mb);

        // Destroy existing chart if it exists
        if (growthChart) {
            growthChart.destroy();
        }

        // Create stacked area chart showing storage by category
        growthChart = new Chart(chartEl, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Check Results',
                        data: resultsData,
                        borderColor: 'rgba(14, 165, 233, 1)',
                        backgroundColor: 'rgba(14, 165, 233, 0.5)',
                        borderWidth: 1,
                        tension: 0.3,
                        fill: 'origin',
                        pointRadius: 0,
                        pointHoverRadius: 4,
                        stack: 'storage',
                    },
                    {
                        label: 'Artifacts',
                        data: artifactsData,
                        borderColor: 'rgba(168, 85, 247, 1)',
                        backgroundColor: 'rgba(168, 85, 247, 0.5)',
                        borderWidth: 1,
                        tension: 0.3,
                        fill: '-1',
                        pointRadius: 0,
                        pointHoverRadius: 4,
                        stack: 'storage',
                    },
                    {
                        label: 'Checks',
                        data: checksData,
                        borderColor: 'rgba(16, 185, 129, 1)',
                        backgroundColor: 'rgba(16, 185, 129, 0.5)',
                        borderWidth: 1,
                        tension: 0.3,
                        fill: '-1',
                        pointRadius: 0,
                        pointHoverRadius: 4,
                        stack: 'storage',
                    },
                    {
                        label: 'Agent Metrics',
                        data: agentMetricsData,
                        borderColor: 'rgba(251, 191, 36, 1)',
                        backgroundColor: 'rgba(251, 191, 36, 0.5)',
                        borderWidth: 1,
                        tension: 0.3,
                        fill: '-1',
                        pointRadius: 0,
                        pointHoverRadius: 4,
                        stack: 'storage',
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    mode: 'index',
                    intersect: false,
                },
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                        labels: {
                            color: '#cbd5e1',
                            padding: 15,
                            font: {
                                size: 12
                            },
                            usePointStyle: true,
                        }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(15, 23, 42, 0.95)',
                        titleColor: '#f1f5f9',
                        bodyColor: '#cbd5e1',
                        borderColor: 'rgba(14, 165, 233, 0.5)',
                        borderWidth: 1,
                        padding: 12,
                        displayColors: true,
                        callbacks: {
                            label: function(context) {
                                const value = context.parsed.y;
                                return `${context.dataset.label}: ${value.toFixed(2)} MB`;
                            },
                            footer: function(tooltipItems) {
                                let total = 0;
                                tooltipItems.forEach(item => {
                                    total += item.parsed.y;
                                });
                                return `Total: ${total.toFixed(2)} MB`;
                            }
                        },
                        footerColor: '#fbbf24',
                        footerFont: {
                            weight: 'bold',
                            size: 13
                        }
                    }
                },
                scales: {
                    y: {
                        stacked: true,
                        beginAtZero: true,
                        grid: {
                            color: 'rgba(255, 255, 255, 0.05)',
                            drawBorder: false,
                        },
                        ticks: {
                            color: '#94a3b8',
                            callback: function(value) {
                                return value.toFixed(0) + ' MB';
                            }
                        },
                        title: {
                            display: true,
                            text: 'Storage (MB)',
                            color: '#94a3b8',
                            font: {
                                size: 12,
                                weight: 'bold'
                            }
                        }
                    },
                    x: {
                        grid: {
                            color: 'rgba(255, 255, 255, 0.05)',
                            drawBorder: false,
                        },
                        ticks: {
                            color: '#94a3b8',
                            maxRotation: 45,
                            minRotation: 45,
                            autoSkip: true,
                            maxTicksLimit: hours <= 24 ? 12 : 20,
                        }
                    }
                }
            }
        });
    } catch (error) {
        console.error('Failed to load growth chart:', error);
    }
}

/**
 * Initialize event listeners
 */
function initDatabaseHealthChart() {
    // Initialize chart with default range (7 days)
    updateGrowthChart(168);

    // Listen for time range changes
    const rangeSelect = document.getElementById('growth-time-range');
    if (rangeSelect) {
        rangeSelect.addEventListener('change', (e) => {
            const hours = parseInt(e.target.value);
            updateGrowthChart(hours);
        });
    }
}

/**
 * Initialize database health module
 */
export function init() {
    // Initialize chart immediately if on database health page
    initDatabaseHealthChart();

    // Also initialize when HTMX swaps content (for refresh button)
    document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.detail.target.id === 'health-metrics') {
            // Small delay to ensure DOM is ready
            setTimeout(initDatabaseHealthChart, 100);
        }
    });
}
