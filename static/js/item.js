const downloadsTable = document.querySelector(".item-downloads");
const FILE_POLL_MS = 1000;
const BYTE_UNITS = ["B", "kB", "MB", "GB", "TB"];
let filePolling = false;

function formatText(template, values = {}) {
    return template.replace(/\{(\w+)\}/g, (match, key) => (key in values ? values[key] : match));
}

function formatBytes(bytes) {
    let value = bytes;
    let unit = 0;
    while (value >= 1000 && unit < BYTE_UNITS.length - 1) {
        value /= 1000;
        unit += 1;
    }
    return `${value.toFixed(unit ? 1 : 0)} ${BYTE_UNITS[unit]}`;
}

function fileRow(name) {
    return [...downloadsTable.querySelectorAll("tr[data-name]")].find((row) => row.dataset.name === name);
}

function showFileJob(row, job) {
    const button = row.querySelector(".save-file");
    const progress = row.querySelector(".file-progress");
    const status = row.querySelector(".file-status");
    const messages = downloadsTable.dataset;
    progress.hidden = job.state !== "running";
    status.classList.toggle("has-text-danger", job.state === "error");
    if (job.state === "running") {
        button.disabled = true;
        if (job.total) {
            progress.max = job.total;
            progress.value = job.done;
        } else {
            progress.removeAttribute("value");
        }
        status.textContent = formatText(job.total ? messages.progress : messages.progressUnknown, {
            done: formatBytes(job.done),
            total: formatBytes(job.total),
        });
    } else if (job.state === "done") {
        button.disabled = true;
        if (row.querySelector(".rqbit-status")) {
            button.textContent = "Sent to rqbit";
            status.textContent = "Torrent sent to rqbit.";
            window.refreshRqbitStatus?.(row.querySelector(".rqbit-status"));
        } else {
            button.textContent = messages.savedLabel;
            status.textContent = messages.saved;
        }
    } else if (job.state === "error") {
        button.disabled = false;
        status.textContent = formatText(messages.failed, { error: job.error });
    }
}

async function pollFiles() {
    if (filePolling) return;
    filePolling = true;
    try {
        while (true) {
            const response = await fetch(downloadsTable.dataset.filesUrl);
            const data = await response.json();
            let running = false;
            for (const [name, job] of Object.entries(data.files)) {
                const row = fileRow(name);
                if (row) showFileJob(row, job);
                running ||= job.state === "running";
            }
            if (!running) break;
            await new Promise((resolve) => setTimeout(resolve, FILE_POLL_MS));
        }
    } finally {
        filePolling = false;
    }
}

if (downloadsTable) {
    for (const row of downloadsTable.querySelectorAll("tr[data-name]")) {
        const button = row.querySelector(".save-file");
        button.addEventListener("click", async () => {
            button.disabled = true;
            try {
                const response = await fetch(downloadsTable.dataset.filesUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ name: row.dataset.name }),
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error);
                showFileJob(row, data);
                pollFiles();
            } catch (error) {
                showFileJob(row, {
                    state: "error",
                    error: error.message || downloadsTable.dataset.requestFailed,
                });
            }
        });
    }
    pollFiles();
}
