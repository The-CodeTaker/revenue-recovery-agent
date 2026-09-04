document.addEventListener("DOMContentLoaded", async () => {
    try {
        const response = await fetch('/api/metrics');
        if (!response.ok) throw new Error("Metrics not found");
        
        const data = await response.json();
        let isEngineMode = true; // Default state

        // DOM Elements for Toggle
        const engineToggle = document.getElementById('engineToggle');
        const engineLabel = document.getElementById('engineLabel');
        const naiveLabel = document.getElementById('naiveLabel');
        
        // DOM Elements for Stat Cards
        const statFailed = document.getElementById('stat-failed');
        const stat2Title = document.getElementById('stat-title-2');
        const stat2Value = document.getElementById('stat-naive'); 
        const stat3Title = document.getElementById('stat-title-3');
        const stat3Value = document.getElementById('stat-engine');
        const stat4Title = document.getElementById('stat-title-4');
        const stat4Value = document.getElementById('stat-improvement');
        
        const card3 = document.getElementById('card-3');
        const card4 = document.getElementById('card-4');

        // Always set Total Failed Revenue (Static)
        statFailed.textContent = `₹${data.total_failed_revenue_inr.toLocaleString()}`;

        // Initialize Chart
        const ctx = document.getElementById('recoveryChart').getContext('2d');
        const chart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: ['Naive Baseline', 'AI Engine Expected'],
                datasets: [{
                    label: 'Recovered Revenue (₹)',
                    data: [data.naive_baseline.recovered_revenue_inr, data.engine_recovery.recovered_revenue_inr],
                    backgroundColor: ['#334155', '#3B82F6'], // Base colors
                    borderRadius: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    y: { beginAtZero: true, grid: { color: '#334155' }, ticks: { color: '#94A3B8' } },
                    x: { grid: { display: false }, ticks: { color: '#94A3B8' } }
                }
            }
        });

        // Update UI Function
        const updateUI = () => {
            const totalFailed = data.total_failed_revenue_inr;

            if (isEngineMode) {
                // UI State: AI Engine
                engineLabel.classList.add('active');
                naiveLabel.classList.remove('active');
                
                card3.classList.add('highlight');
                card4.classList.add('success');

                stat2Title.textContent = 'Engine Win Rate';
                const engineRate = ((data.engine_recovery.recovered_revenue_inr / totalFailed) * 100).toFixed(1);
                stat2Value.textContent = `${engineRate}%`;
                
                stat3Title.textContent = 'Expected Recovery';
                stat3Value.textContent = `₹${data.engine_recovery.recovered_revenue_inr.toLocaleString()}`;
                
                stat4Title.textContent = 'Improvement';
                stat4Value.textContent = `+${data.improvement_over_naive_baseline_pct}%`;
                
                // Highlight Engine bar, dim Naive bar
                chart.data.datasets[0].backgroundColor = ['#334155', '#3B82F6'];
            } else {
                // UI State: Naive Baseline
                engineLabel.classList.remove('active');
                naiveLabel.classList.add('active');
                
                card3.classList.remove('highlight');
                card4.classList.remove('success');

                stat2Title.textContent = 'Naive Win Rate';
                stat2Value.textContent = `${(data.naive_baseline.assumed_flat_recovery_rate * 100).toFixed(1)}%`;
                
                stat3Title.textContent = 'Baseline Recovery';
                stat3Value.textContent = `₹${data.naive_baseline.recovered_revenue_inr.toLocaleString()}`;
                
                stat4Title.textContent = 'Improvement';
                stat4Value.textContent = 'Baseline';
                
                // Highlight Naive bar, dim Engine bar
                chart.data.datasets[0].backgroundColor = ['#3B82F6', '#334155'];
            }
            chart.update();
        };

        // Listen for Toggle Flips
        engineToggle.addEventListener('change', (e) => {
            isEngineMode = e.target.checked;
            updateUI();
        });

        // Run once on load
        updateUI();

    } catch (err) {
        console.error("Failed to load metrics:", err);
    }
});
// --- Unresolved / Not Retried Panel -------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
    const loading = document.getElementById('unresolved-loading');
    const errorBox = document.getElementById('unresolved-error');
    const tbody = document.getElementById('unresolved-tbody');
    const countBadge = document.getElementById('unresolved-count');

    // Categorizes a record into one of the 5 named reason buckets, based
    // purely on fields already present in unresolved_log.json.
    function classifyReasonTag(record) {
        const reasoning = (record.retry_reasoning || "").toLowerCase();
        const blockReason = (record.block_reason || "").toLowerCase();

        if (!record.is_allowed) {
            if (blockReason.includes("dispute")) {
                return { label: "Active Dispute", cls: "dispute" };
            }
            if (blockReason.includes("blackout") || blockReason.includes("cooldown")) {
                return { label: "Cooldown / Blackout", cls: "cooldown" };
            }
        }

        // Check the boolean flag directly!
        if (record.opt_out_flag) {
            return { label: "Opt-Out", cls: "optout" };
        }
        if (reasoning.includes("retry cap") || reasoning.includes("maximum retry")) {
            return { label: "Hit Attempt Cap", cls: "cap" };
        }
        if (record.classified_reason === "card_expired") {
            return { label: "Expired Card", cls: "expired" };
        }
        return { label: "Other", cls: "other" };
    }
    function renderRow(record) {
        const tag = classifyReasonTag(record);
        const tr = document.createElement('tr');

        const cells = [
            record.customer_id || 'N/A',
            record.amount != null ? `₹${Number(record.amount).toLocaleString()}` : 'N/A',
            record.failure_reason || 'N/A',
        ];

        cells.forEach(text => {
            const td = document.createElement('td');
            td.textContent = text; // XSS-safe: textContent only
            tr.appendChild(td);
        });

        const tagTd = document.createElement('td');
        const tagSpan = document.createElement('span');
        tagSpan.className = `reason-tag ${tag.cls}`;
        tagSpan.textContent = tag.label;
        tagTd.appendChild(tagSpan);
        tr.appendChild(tagTd);

        const actionTd = document.createElement('td');
        actionTd.textContent = record.proposed_action || 'N/A';
        tr.appendChild(actionTd);

        const detailTd = document.createElement('td');
        detailTd.textContent = record.block_reason || record.retry_reasoning || 'N/A';
        tr.appendChild(detailTd);

        return tr;
    }

    try {
        loading.classList.remove('hidden');
        const response = await fetch('/api/unresolved');
        loading.classList.add('hidden');

        if (!response.ok) {
            throw new Error(`Server returned ${response.status}`);
        }

        const records = await response.json();
        countBadge.textContent = `${records.length} record${records.length === 1 ? '' : 's'}`;

        if (!records.length) {
            const emptyRow = document.createElement('tr');
            const emptyTd = document.createElement('td');
            emptyTd.colSpan = 6;
            emptyTd.className = 'empty-state';
            emptyTd.textContent = 'No unresolved records — everything is retried or clear.';
            emptyRow.appendChild(emptyTd);
            tbody.appendChild(emptyRow);
            return;
        }

        records.forEach(record => tbody.appendChild(renderRow(record)));

    } catch (err) {
        loading.classList.add('hidden');
        errorBox.textContent = "Failed to load unresolved records.";
        errorBox.classList.remove('hidden');
        console.error("Unresolved panel error:", err);
    }
});