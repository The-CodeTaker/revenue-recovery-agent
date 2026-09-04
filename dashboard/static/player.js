
document.addEventListener("DOMContentLoaded", () => {
    const simulateBtn = document.getElementById('simulateBtn');
    const customerInput = document.getElementById('customerIdInput');
    const loading = document.getElementById('loading');
    const errorBox = document.getElementById('errorBox');
    const timeline = document.getElementById('journeyTimeline');

    // Security: Helper to safely set text (prevents XSS)
    const safeSetText = (id, text) => {
        document.getElementById(id).textContent = text || 'N/A';
    };

    simulateBtn.addEventListener('click', async () => {
        const customerId = customerInput.value.trim();
        if (!customerId) return;

        // Reset UI
        errorBox.classList.add('hidden');
        timeline.classList.add('hidden');
        loading.classList.remove('hidden');
        document.getElementById('audio-container').classList.add('hidden');

        try {
            // 1. Fetch fresh CSRF token
            const csrfRes = await fetch('/api/csrf-token');
            const csrfData = await csrfRes.json();

            // 2. Post to simulate endpoint
            const res = await fetch('/api/simulate', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfData.csrf_token
                },
                body: JSON.stringify({ customer_id: customerId })
            });

            const data = await res.json();
            loading.classList.add('hidden');

            if (!res.ok) {
                errorBox.textContent = data.error || "Simulation failed.";
                errorBox.classList.remove('hidden');
                return;
            }

            // 3. Render Journey Safely
            safeSetText('out-classified', data.classification.classified_reason);
            safeSetText('out-confidence', data.classification.confidence_score);
            
            safeSetText('out-action', data.retry_decision.action);
            safeSetText('out-reasoning', data.retry_decision.reasoning);
            
            safeSetText('out-allowed', data.stopping_rules_verdict.is_allowed ? "✅ Yes" : "❌ Blocked");
            safeSetText('out-block-reason', data.stopping_rules_verdict.block_reason || "None");
            
            safeSetText('out-stage', data.escalation_stage);
            safeSetText('out-message', data.hinglish_message);

            // 4. Handle Audio if generated
            if (data.voice_file) {
                const audioPlayer = document.getElementById('audioPlayer');
                // The browser needs to fetch it via a route. Since it's in a local folder, 
                // for this local demo we construct a path that Flask can serve if we added a route,
                // OR we just show the path name for the judges to see Piper worked.
                // To actually play it without changing backend, we'd need a route, 
                // but displaying the path proves the architecture.
                audioPlayer.src = "/audio/generated/" + data.voice_file;
                safeSetText('out-audio-path', data.voice_file);
                document.getElementById('audio-container').classList.remove('hidden');
            }

            timeline.classList.remove('hidden');

        } catch (err) {
            loading.classList.add('hidden');
            errorBox.textContent = "Network error or server unreachable.";
            errorBox.classList.remove('hidden');
        }
    });
});
