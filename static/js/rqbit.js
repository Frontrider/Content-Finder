const RQBIT_POLL_MS = 2000;

function formatRqbitBytes(bytes) {
    let value = bytes;
    let unit = 0;
    const units = ["B", "kB", "MB", "GB", "TB"];
    while (value >= 1000 && unit < units.length - 1) {
        value /= 1000;
        unit += 1;
    }
    return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function showRqbitStatus(element, status) {
    const container = element.closest("form") || element.closest("tr");
    const button = container.querySelector("[data-rqbit-button]");
    const label = element.dataset.rqbitLabel;
    const state = status.state;
    const total = status.total_bytes || 0;
    const progress = status.progress_bytes || 0;
    const percent = total ? Math.min(100, Math.floor(progress * 100 / total)) : 0;
    const hasTorrent = !["not_added", "not_found"].includes(state);

    button.disabled = hasTorrent;
    button.textContent = hasTorrent ? `rqbit: ${state}` : label;
    if (state === "not_added") {
        element.textContent = "Not added to rqbit.";
    } else if (state === "not_found") {
        element.textContent = "Torrent is no longer in rqbit.";
    } else if (status.finished) {
        element.textContent = `Complete · ${formatRqbitBytes(total)}`;
    } else if (total) {
        element.textContent = `${state} · ${percent}% · ${formatRqbitBytes(progress)} of ${formatRqbitBytes(total)}`;
    } else {
        element.textContent = state;
    }
    if (status.error) {
        element.textContent += ` · ${status.error}`;
    }

    return state === "initializing" || (state === "live" && !status.finished);
}

async function pollRqbitStatus(element) {
    try {
        const response = await fetch(element.dataset.rqbitStatusUrl);
        const status = await response.json();
        if (!response.ok) {
            throw new Error(status.error || "Unable to check rqbit status.");
        }
        if (showRqbitStatus(element, status)) {
            window.setTimeout(() => pollRqbitStatus(element), RQBIT_POLL_MS);
        }
    } catch (error) {
        element.textContent = error.message || "Unable to check rqbit status.";
    }
}

window.refreshRqbitStatus = pollRqbitStatus;

for (const element of document.querySelectorAll(".rqbit-status")) {
    pollRqbitStatus(element);
}
