const ChartUtils = {
    emotionChartInstance: null,
    engagementChartInstance: null,
    attendanceChartInstance: null,

    getThemeColors() {
        if (typeof getComputedStyle === 'undefined') {
            return {
                primary: '#3b82f6',
                success: '#22c55e',
                warning: '#f59e0b',
                danger: '#ef4444',
                info: '#0ea5e9',
                border: '#e5e7eb',
                text: '#1f2937'
            };
        }
        const style = getComputedStyle(document.body);
        return {
            primary: style.getPropertyValue('--primary-color').trim() || '#3b82f6',
            success: style.getPropertyValue('--success-color').trim() || '#22c55e',
            warning: style.getPropertyValue('--warning-color').trim() || '#f59e0b',
            danger: style.getPropertyValue('--danger-color').trim() || '#ef4444',
            info: style.getPropertyValue('--info-color').trim() || '#0ea5e9',
            border: style.getPropertyValue('--border-color').trim() || '#e5e7eb',
            text: style.getPropertyValue('--text-primary').trim() || '#1f2937'
        };
    },

    createEmotionChart() {
        const canvas = document.getElementById('emotionChart');
        if (!canvas) return;

        const ctx = canvas.getContext('2d');
        const colors = this.getThemeColors();

        // Check if there is backend data
        let chartData = [15, 10, 5, 2];
        let labels = ['Happy', 'Neutral', 'Confused', 'Bored'];
        
        if (window._backendEmotionData) {
            labels = Object.keys(window._backendEmotionData);
            chartData = Object.values(window._backendEmotionData);
        } else if (window.SmartClassMonitor && window.SmartClassMonitor.classroomData && window.SmartClassMonitor.classroomData.emotions) {
            const em = window.SmartClassMonitor.classroomData.emotions;
            chartData = [em.happy, em.neutral, em.confused, em.bored];
        }

        if (this.emotionChartInstance) {
            this.emotionChartInstance.destroy();
        }

        this.emotionChartInstance = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Emotion Distribution',
                    data: chartData,
                    backgroundColor: [colors.success, colors.info, colors.warning, colors.danger],
                    borderWidth: 0,
                    hoverOffset: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'right',
                        labels: {
                            color: colors.text,
                            font: {
                                family: 'Inter, sans-serif'
                            }
                        }
                    }
                },
                cutout: '70%'
            }
        });
    },

    createEngagementChart() {
        const canvas = document.getElementById('engagementChart');
        if (!canvas) return;

        const ctx = canvas.getContext('2d');
        const colors = this.getThemeColors();

        let labels = ['10:00', '10:05', '10:10', '10:15', '10:20', '10:25', '10:30'];
        let dataset1 = [65, 75, 70, 80, 85, 82, 90];
        let dataset2 = [60, 65, 75, 72, 80, 85, 88];

        if (window._backendTimeline) {
            labels = window._backendTimeline.map(t => t.time);
            dataset1 = window._backendTimeline.map(t => t.engagement);
            dataset2 = window._backendTimeline.map(t => t.attention || Math.max(0, t.engagement - 5));
        }

        if (this.engagementChartInstance) {
            this.engagementChartInstance.destroy();
        }

        // Add a smooth background gradient
        let gradient;
        try {
            gradient = ctx.createLinearGradient(0, 0, 0, 400);
            gradient.addColorStop(0, 'rgba(59, 130, 246, 0.5)');   
            gradient.addColorStop(1, 'rgba(59, 130, 246, 0.0)');
        } catch(e) { gradient = colors.primary; }

        this.engagementChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Engagement',
                        data: dataset1,
                        borderColor: colors.primary,
                        backgroundColor: gradient,
                        borderWidth: 3,
                        tension: 0.4,
                        fill: true,
                        pointBackgroundColor: '#ffffff',
                        pointBorderColor: colors.primary,
                        pointBorderWidth: 2,
                        pointRadius: 4,
                        pointHoverRadius: 6
                    },
                    {
                        label: 'Attention',
                        data: dataset2,
                        borderColor: colors.success,
                        borderWidth: 2,
                        tension: 0.4,
                        borderDash: [5, 5],
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 5
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
                        position: 'top',
                        labels: { color: colors.text, usePointStyle: true, boxWidth: 8 }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(0,0,0,0.8)',
                        titleColor: '#fff',
                        bodyColor: '#fff',
                        padding: 10,
                        cornerRadius: 8,
                        displayColors: true
                    }
                },
                scales: {
                    x: {
                        grid: { display: false, drawBorder: false },
                        ticks: { color: colors.text, font: { size: 11 } }
                    },
                    y: {
                        beginAtZero: true,
                        max: 100,
                        grid: { color: colors.border, borderDash: [5, 5], drawBorder: false },
                        ticks: { color: colors.text, stepSize: 20, font: { size: 11 } }
                    }
                }
            }
        });
    },

    createAttendanceChart() {
        const canvas = document.getElementById('attendanceChart');
        if (!canvas) return;

        const ctx = canvas.getContext('2d');
        const colors = this.getThemeColors();

        if (this.attendanceChartInstance) {
            this.attendanceChartInstance.destroy();
        }
        
        this.attendanceChartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
                datasets: [
                    {
                        label: 'Present',
                        data: [22, 21, 23, 22, 24],
                        backgroundColor: colors.success,
                        borderRadius: 4
                    },
                    {
                        label: 'Absent',
                        data: [2, 3, 1, 2, 0],
                        backgroundColor: colors.danger,
                        borderRadius: 4
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'top',
                        labels: { color: colors.text, usePointStyle: true, boxWidth: 8 }
                    }
                },
                scales: {
                    x: {
                        stacked: true,
                        grid: { display: false, drawBorder: false },
                        ticks: { color: colors.text }
                    },
                    y: {
                        stacked: true,
                        beginAtZero: true,
                        max: 24,
                        grid: { color: colors.border, borderDash: [5, 5], drawBorder: false },
                        ticks: { color: colors.text, stepSize: 4 }
                    }
                }
            }
        });
    }
};

window.ChartUtils = ChartUtils;

// Wrapper functions used by main.js
window.updateEmotionChart = function() {
    if (window.ChartUtils) {
        window.ChartUtils.createEmotionChart();
    }
};

window.updateEngagementChart = function() {
    if (window.ChartUtils) {
        window.ChartUtils.createEngagementChart();
    }
};

// Listen for updates from backend
window.addEventListener('liveDataUpdate', function(e) {
    if (e.detail && e.detail.emotion_distribution) {
        window._backendEmotionData = e.detail.emotion_distribution;
        window.updateEmotionChart();
    }
    if (e.detail && e.detail.timeline) {
        window._backendTimeline = e.detail.timeline;
        window.updateEngagementChart();
    }
});
