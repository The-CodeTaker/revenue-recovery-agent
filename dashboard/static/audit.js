
document.addEventListener("DOMContentLoaded", () => {
    const searchBtn = document.getElementById('auditSearchBtn');
    const searchInput = document.getElementById('auditSearchInput');
    const loading = document.getElementById('audit-loading');
    const errorBox = document.getElementById('audit-error');
    const tbody = document.getElementById('audit-tbody');
    const countBadge = document.getElementById('audit-count');

    // Security: build cells via textContent, never innerHTML -- audit
    // entries carry customer-controlled / LLM-generated text
    // (message_sent, failure_reason, etc.) that must never be
    // interpreted as markup by the browser.
    const cell = (text) => {
        const td = document.createElement('td');
        td.textContent = (text === null || text === undefined || text === '') ? 'N/A' : text;
        return td;
    };

    const formatStoppingRules = (entry) => {
        const verdict = entry.stopping_rules_verdict || {};
        if (verdict.is_allowed) return '✅ Allowed';
        return `❌ Blocked (${verdict.block_reason || 'unknown'})`;
    };

    const renderRows = (entries) => {
        tbody.innerHTML = '';
        entries.forEach((entry) => {
            const tr = document.createElement('tr');
            tr.appendChild(cell(entry.timestamp));
            tr.appendChild(cell(entry.source));
            tr.appendChild(cell(entry.customer_id));
            tr.appendChild(cell(entry.classification ? entry.classification.classified_reason : null));
            tr.appendChild(cell(entry.proposed_action));
            tr.appendChild(cell(formatStoppingRules(entry)));
            tr.appendChild(cell(entry.effective_action));

            const messageTd = cell(entry.message_sent);
            messageTd.classList.add('message-cell');
            tr.appendChild(messageTd);

            tbody.appendChild(tr);
        });
    };

    const runSearch = async () => {
        const customerId = searchInput.value.trim();

        errorBox.classList.add('hidden');
        loading.classList.remove('hidden');

        try {
            const params = new URLSearchParams();
            if (customerId) params.set('customer_id', customerId);
            params.set('limit', '50');

            const res = await fetch(`/api/audit-trail?${params.toString()}`);
            const data = await res.json();

            loading.classList.add('hidden');

            if (!res.ok) {
                errorBox.textContent = data.error || 'Audit trail search failed.';
                errorBox.classList.remove('hidden');
                return;
            }

            renderRows(data.entries || []);
            countBadge.textContent = `${data.count} entries shown`;
        } catch (err) {
            loading.classList.add('hidden');
            errorBox.textContent = 'Network error or server unreachable.';
            errorBox.classList.remove('hidden');
        }
    };

    searchBtn.addEventListener('click', runSearch);
    searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') runSearch();
    });

    // Load the most recent entries on page load so the panel isn't
    // empty before the operator searches for anything.
    runSearch();
});
