const form = document.querySelector("#search-form");
const sourceInput = document.querySelector("#search-source");
const queryInput = document.querySelector("#query");
const contentTypeInput = document.querySelector("#content-type");
const contentTypeField = document.querySelector("#content-type-field");
const languageInput = document.querySelector("#language");
const languageField = document.querySelector("#language-field");
const languageSearch = document.querySelector("#language-search");
const languageSearchStatus = document.querySelector("#language-search-status");
const languageOptions = [...languageInput.options].map((option) => ({
    value: option.value,
    text: option.text,
}));

function formatText(template, values = {}) {
    return template.replace(/\{(\w+)\}/g, (match, key) => (key in values ? String(values[key]) : match));
}

function message(element, key, values) {
    return formatText(element.dataset[key] || "", values);
}

function cloneTemplate(id) {
    return document.querySelector(`#${id}`).content.firstElementChild.cloneNode(true);
}

languageSearch.addEventListener("input", () => {
    const query = languageSearch.value.trim().toLocaleLowerCase();
    const selected = languageInput.value;
    const matches = languageOptions.filter((option) =>
        option.value && `${option.text} ${option.value}`.toLocaleLowerCase().includes(query)
    );
    const matchingValues = new Set(matches.map((option) => option.value));
    const visibleOptions = languageOptions.filter((option) =>
        !option.value || option.value === selected || matchingValues.has(option.value)
    );
    languageInput.replaceChildren(
        ...visibleOptions.map((option) => new Option(option.text, option.value))
    );
    languageInput.value = selected;
    languageSearchStatus.textContent = query
        ? message(languageSearchStatus, selected ? "matchesRetained" : "matches", { count: matches.length })
        : "";
});
const categoryInput = document.querySelector("#category");
const categoryField = document.querySelector("#category-field");
const searchButton = document.querySelector("#search-button");
const status = document.querySelector("#status");
const resultsElement = document.querySelector("#results");
const pagination = document.querySelector("#pagination");
const previousButton = document.querySelector("#previous-page");
const nextButton = document.querySelector("#next-page");
const pageCount = document.querySelector("#page-count");
const DESCRIPTION_MAX_LENGTH = 200;
const urlParameters = new URLSearchParams(window.location.search);
const topicSidebar = document.querySelector("#topic-sidebar");
const topicForm = document.querySelector("#topic-form");
const topicInput = document.querySelector("#topic-input");
const topicButton = document.querySelector("#topic-button");
const topicStatus = document.querySelector("#topic-status");
const collectionSidebar = document.querySelector("#collection-sidebar");
const collectionStatus = document.querySelector("#collection-status");
const gutenbergSidebar = document.querySelector("#gutenberg-sidebar");
const gutenbergButton = document.querySelector("#gutenberg-update");
const gutenbergProgress = document.querySelector("#gutenberg-progress");
const gutenbergStatus = document.querySelector("#gutenberg-status");
const GUTENBERG_POLL_MS = 1000;
let gutenbergPolling = false;
const gutenbergTagSidebar = document.querySelector("#gutenberg-tag-sidebar");
let currentPage = 1;
let totalPages = 0;

function createTagFilter(id, includeParameter, excludeParameter) {
    const searchInput = document.querySelector(`#${id}-search`);
    const select = document.querySelector(`#${id}`);
    const statusElement = document.querySelector(`#${id}-status`);
    const list = document.querySelector(`#${id}-list`);
    const placeholder = select.options[0];
    const included = new Set(urlParameters.getAll(includeParameter));
    const excluded = new Set(urlParameters.getAll(excludeParameter));
    for (const value of excluded) included.delete(value);
    let options = [];

    function changed() {
        renderChips();
        renderOptions();
        updateSourceFields();
        search(1);
    }

    function renderOptions() {
        const query = searchInput.value.trim().toLocaleLowerCase();
        const limit = Number(select.dataset.limit);
        const matches = options.filter((option) =>
            !included.has(option.name) && !excluded.has(option.name) &&
            (!query || option.name.toLocaleLowerCase().includes(query))
        );
        select.replaceChildren(
            placeholder,
            ...matches.slice(0, limit).map((option) =>
                new Option(message(select, "option", option), option.name)
            ),
        );
        select.value = "";
        statusElement.classList.remove("has-text-danger");
        statusElement.textContent = !query && matches.length <= limit
            ? ""
            : message(statusElement, matches.length > limit ? "limited" : "matches", {
                count: matches.length,
                limit,
            });
    }

    function renderChips() {
        list.replaceChildren();
        for (const [value, isIncluded] of [
            ...[...included].map((value) => [value, true]),
            ...[...excluded].map((value) => [value, false]),
        ]) {
            const chip = cloneTemplate("tag-chip-template");
            const name = chip.querySelector(".tag-chip-name");
            const remove = chip.querySelector(".delete");
            chip.classList.add(chip.dataset[isIncluded ? "includeClass" : "excludeClass"]);
            name.textContent = value;
            name.title = message(chip, isIncluded ? "includeLabel" : "excludeLabel", { value });
            name.setAttribute("aria-label", name.title);
            remove.setAttribute("aria-label", message(remove, "label", { value }));
            const toggle = () => {
                (isIncluded ? included : excluded).delete(value);
                (isIncluded ? excluded : included).add(value);
                changed();
            };
            name.addEventListener("click", toggle);
            name.addEventListener("keydown", (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    toggle();
                }
            });
            remove.addEventListener("click", () => {
                included.delete(value);
                excluded.delete(value);
                changed();
            });
            list.append(chip);
        }
    }

    searchInput.addEventListener("input", renderOptions);
    select.addEventListener("change", () => {
        if (!select.value) return;
        included.add(select.value);
        searchInput.value = "";
        changed();
    });
    renderChips();

    return {
        get size() {
            return included.size + excluded.size;
        },
        setOptions(values) {
            options = values.map((value) => (typeof value === "string" ? { name: value } : value));
            renderOptions();
        },
        showError(text) {
            statusElement.textContent = text;
            statusElement.classList.add("has-text-danger");
        },
        appendTo(parameters) {
            for (const value of included) parameters.append(includeParameter, value);
            for (const value of excluded) parameters.append(excludeParameter, value);
        },
    };
}

const topicFilter = createTagFilter("topic-filter", "topic", "exclude_topic");
const collectionFilter = createTagFilter("collection-filter", "collection", "exclude_collection");
const gutenbergTagFilter = createTagFilter("gutenberg-tag", "tag", "exclude_tag");

function appendTopics(parameters) {
    topicFilter.appendTo(parameters);
    collectionFilter.appendTo(parameters);
}

function appendGutenbergTags(parameters) {
    gutenbergTagFilter.appendTo(parameters);
}

async function loadGutenbergTags() {
    try {
        const response = await fetch("/api/gutenberg/tags");
        const data = await response.json();
        if (response.status === 503) return;
        if (!response.ok) throw new Error(data.error);
        gutenbergTagFilter.setOptions(data.tags);
    } catch (error) {
        gutenbergTagFilter.showError(
            error.message || message(document.querySelector("#gutenberg-tag-status"), "loadFailed")
        );
    }
}

function hasArchiveFilters() {
    return topicFilter.size || collectionFilter.size || languageInput.value;
}

async function loadTopics(kind, filter, statusElement) {
    try {
        const response = await fetch(`/api/${kind}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error);
        filter.setOptions(data[kind]);
    } catch (error) {
        statusElement.textContent = error.message || message(statusElement, "loadFailed");
        statusElement.classList.add("has-text-danger");
    }
}

function bindSavedFilter(form, input, button, statusElement, kind, noun, filter) {
form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    statusElement.textContent = message(statusElement, "checking");
    statusElement.classList.remove("has-text-danger");
    try {
        const response = await fetch(`/api/${kind}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ [noun]: input.value.trim() }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error);
        filter.setOptions(data[kind]);
        statusElement.textContent = message(statusElement, "saved", { count: data.total_results });
        input.value = "";
    } catch (error) {
        statusElement.textContent = error.message || message(statusElement, "saveFailed");
        statusElement.classList.add("has-text-danger");
    } finally {
        button.disabled = false;
    }
});
}

bindSavedFilter(topicForm, topicInput, topicButton, topicStatus, "topics", "topic", topicFilter);
bindSavedFilter(
    document.querySelector("#collection-form"),
    document.querySelector("#collection-input"),
    document.querySelector("#collection-button"),
    collectionStatus,
    "collections", "collection",
    collectionFilter,
);

function updateSearchUrl(query, page) {
    const parameters = new URLSearchParams({
        source: sourceInput.value,
        q: query,
        page: String(page),
    });
    if (sourceInput.value === "catalog") {
        if (selectedCategory()) parameters.set("category", selectedCategory());
    } else if (sourceInput.value === "gutenberg") {
        parameters.set("language", languageInput.value);
        appendGutenbergTags(parameters);
    } else {
        parameters.set("type", contentTypeInput.value);
        parameters.set("language", languageInput.value);
        appendTopics(parameters);
    }
    window.history.replaceState(null, "", `/?${parameters}`);
}

function selectedCategory() {
    return categoryInput.value || categoryInput.dataset.initialCategory || "";
}

function setStatus(text, isError = false) {
    status.textContent = text;
    status.classList.toggle("is-danger", isError);
}

function shortenDescription(description, container) {
    container.innerHTML = description;
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) node.textContent = node.textContent.replace(/\s+/g, " ");
    const text = container.textContent;
    if (text.length <= DESCRIPTION_MAX_LENGTH) return container;

    const excerpt = text.slice(0, DESCRIPTION_MAX_LENGTH - 1);
    const lastSpace = excerpt.lastIndexOf(" ");
    let remaining = lastSpace > DESCRIPTION_MAX_LENGTH * 0.75 ? lastSpace : excerpt.length;
    let truncated = false;
    for (const node of nodes) {
        if (truncated) {
            node.remove();
        } else if (node.textContent.length > remaining) {
            node.textContent = `${node.textContent.slice(0, remaining)}…`;
            remaining = 0;
            truncated = true;
        } else {
            remaining -= node.textContent.length;
        }
    }
    return container;
}

function showResults(items) {
    resultsElement.replaceChildren();
    for (const item of items) {
        const card = cloneTemplate("result-template");
        const link = card.querySelector(".result-link");
        if (item.source === "gutenberg") {
            link.href = `https://www.gutenberg.org/ebooks/${encodeURIComponent(item.identifier)}`;
            link.target = "_blank";
            link.rel = "noopener";
        } else if (item.source === "catalog") {
            const returnParameters = new URLSearchParams({
                q: queryInput.value.trim(),
                source: "catalog",
                category: selectedCategory(),
                page: String(currentPage),
            });
            link.href = `/catalog/${encodeURIComponent(item.identifier)}?${returnParameters}`;
        } else {
            const returnParameters = new URLSearchParams({
                q: queryInput.value.trim(),
                type: contentTypeInput.value,
                language: languageInput.value,
                page: String(currentPage),
            });
            appendTopics(returnParameters);
            link.href = `/item/${encodeURIComponent(item.identifier)}?${returnParameters}`;
        }
        link.textContent = item.title || item.identifier;

        const metadataElement = card.querySelector(".result-metadata");
        const { separator, listSeparator } = metadataElement.dataset;
        const metadata = item.source === "catalog"
            ? item.categories.join(separator)
            : item.source === "gutenberg"
                ? [item.authors.join(listSeparator), item.languages.join(listSeparator), item.year]
                    .filter(Boolean).join(separator)
                : [item.mediatype, item.year].filter(Boolean).join(separator);
        metadataElement.textContent = metadata;
        metadataElement.hidden = !metadata;

        if (item.source !== "catalog" && item.topics.length) {
            const tags = card.querySelector(".result-topics");
            for (const topic of item.topics) {
                const tag = cloneTemplate("result-tag-template");
                tag.textContent = topic;
                tags.append(tag);
            }
            tags.hidden = false;
        }
        if (item.source === "catalog") {
            const torrents = card.querySelector(".result-torrents");
            const count = item.torrents.length;
            torrents.textContent = message(torrents, count === 1 ? "one" : "many", { count });
            torrents.hidden = false;
        } else if (item.description) {
            const summary = card.querySelector(".result-summary");
            shortenDescription(item.description, summary);
            summary.hidden = false;
        }
        resultsElement.append(card);
    }
}

async function search(page) {
    const query = queryInput.value.trim();
    if (!query && sourceInput.value === "archive" && !hasArchiveFilters()) {
        resultsElement.replaceChildren();
        pagination.hidden = true;
        updateSearchUrl(query, 1);
        setStatus(message(status, "emptyQuery"), true);
        return;
    }

    updateSearchUrl(query, page);
    searchButton.disabled = true;
    previousButton.disabled = true;
    nextButton.disabled = true;
    pagination.hidden = true;
    resultsElement.replaceChildren();
    setStatus(message(status, "searching", { source: sourceInput.selectedOptions[0].text }));

    try {
        const parameters = new URLSearchParams({
            source: sourceInput.value,
            q: query,
            page: String(page),
        });
        if (sourceInput.value === "catalog") {
            parameters.set("category", selectedCategory());
        } else if (sourceInput.value === "gutenberg") {
            parameters.set("language", languageInput.value);
            appendGutenbergTags(parameters);
        } else {
            parameters.set("type", contentTypeInput.value);
            parameters.set("language", languageInput.value);
            appendTopics(parameters);
        }
        const response = await fetch(`/api/search?${parameters}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || message(status, "failed"));

        currentPage = data.page;
        totalPages = data.total_pages;
        if (sourceInput.value === "catalog") {
            categoryInput.replaceChildren(categoryInput.options[0]);
            for (const category of data.categories) {
                categoryInput.add(new Option(category, category));
            }
            categoryInput.value = data.selected_category;
            delete categoryInput.dataset.initialCategory;
            updateSearchUrl(query, currentPage);
        }
        showResults(data.results);
        if (data.total_results === 0) {
            setStatus(message(status, query || languageInput.value ? "noResults" : "emptyCatalog"));
        } else {
            setStatus(message(status, "showing", {
                start: (currentPage - 1) * data.page_size + 1,
                end: Math.min(currentPage * data.page_size, data.total_results),
                total: data.total_results,
                noun: sourceInput.selectedOptions[0].dataset.noun,
            }));
        }

        pagination.hidden = totalPages <= 1;
        pageCount.textContent = message(pageCount, "label", { page: currentPage, total: totalPages });
        previousButton.disabled = currentPage <= 1;
        nextButton.disabled = currentPage >= totalPages;
    } catch (error) {
        setStatus(error.message || message(status, "failed"), true);
    } finally {
        searchButton.disabled = false;
    }
}

form.addEventListener("submit", (event) => {
    event.preventDefault();
    search(1);
});
const initialParameters = new URLSearchParams(window.location.search);
const initialSource = initialParameters.get("source") || "archive";
const initialQuery = initialParameters.get("q") || "";
const initialType = initialParameters.get("type") || "all";
const initialLanguage = initialParameters.get("language") || "";
const initialCategory = initialParameters.get("category") || "";
const initialPage = Number.parseInt(initialParameters.get("page") || "1", 10);
if (["archive", "catalog", "gutenberg"].includes(initialSource)) {
    sourceInput.value = initialSource;
}
if ([...contentTypeInput.options].some((option) => option.value === initialType)) {
    contentTypeInput.value = initialType;
}
if ([...languageInput.options].some((option) => option.value === initialLanguage)) {
    languageInput.value = initialLanguage;
}
function updateSourceFields() {
    const isCatalog = sourceInput.value === "catalog";
    const isGutenberg = sourceInput.value === "gutenberg";
    const isArchive = !isCatalog && !isGutenberg;
    contentTypeField.hidden = !isArchive;
    languageField.hidden = isCatalog;
    categoryField.hidden = !isCatalog;
    topicSidebar.hidden = !isArchive;
    collectionSidebar.hidden = !isArchive;
    gutenbergSidebar.hidden = !isGutenberg;
    gutenbergTagSidebar.hidden = !isGutenberg;
    queryInput.required = isArchive && !hasArchiveFilters();
    queryInput.placeholder = sourceInput.selectedOptions[0].dataset.placeholder;
}

function formatBytes(bytes) {
    return message(gutenbergStatus, "size", { size: (bytes / (1024 * 1024)).toFixed(1) });
}

function showGutenbergStatus(job) {
    const running = job.state === "running";
    gutenbergButton.disabled = running;
    gutenbergButton.textContent = message(gutenbergButton, job.available ? "updateLabel" : "downloadLabel");
    gutenbergProgress.hidden = !running;
    gutenbergStatus.classList.toggle("has-text-danger", job.state === "error");
    if (running) {
        const percent = job.total ? Math.min(100, (job.done / job.total) * 100) : null;
        if (percent === null) gutenbergProgress.removeAttribute("value");
        else gutenbergProgress.value = percent;
        const key = job.phase === "parse"
            ? "parseProgress"
            : percent === null ? "downloadUnknown" : "downloadProgress";
        gutenbergStatus.textContent = message(gutenbergStatus, key, {
            percent: percent === null ? "" : percent.toFixed(1),
            done: formatBytes(job.done),
            total: formatBytes(job.total),
        });
    } else if (job.state === "error") {
        gutenbergStatus.textContent = message(gutenbergStatus, "failed", { error: job.error });
    } else if (job.state === "done") {
        gutenbergStatus.textContent = message(gutenbergStatus, "done", { books: job.books });
    } else {
        gutenbergStatus.textContent = message(gutenbergStatus, job.available ? "available" : "missing");
    }
}

async function pollGutenberg() {
    if (gutenbergPolling) return;
    gutenbergPolling = true;
    let sawRunning = false;
    try {
        while (true) {
            const response = await fetch("/api/gutenberg/status");
            const job = await response.json();
            showGutenbergStatus(job);
            if (job.state === "running") sawRunning = true;
            else {
                if (sawRunning && job.state === "done") {
                    loadGutenbergTags();
                    if (sourceInput.value === "gutenberg") search(1);
                }
                break;
            }
            await new Promise((resolve) => setTimeout(resolve, GUTENBERG_POLL_MS));
        }
    } catch (error) {
        gutenbergStatus.textContent = message(gutenbergStatus, "pollFailed");
        gutenbergStatus.classList.add("has-text-danger");
    } finally {
        gutenbergPolling = false;
    }
}

gutenbergButton.addEventListener("click", async () => {
    gutenbergButton.disabled = true;
    try {
        const response = await fetch("/api/gutenberg/update", { method: "POST" });
        const data = await response.json();
        if (!response.ok && response.status !== 409) throw new Error(data.error);
    } catch (error) {
        gutenbergStatus.textContent = error.message || message(gutenbergStatus, "startFailed");
        gutenbergStatus.classList.add("has-text-danger");
        gutenbergButton.disabled = false;
        return;
    }
    pollGutenberg();
});
updateSourceFields();
queryInput.value = initialQuery;
if (initialSource === "catalog") {
    categoryInput.dataset.initialCategory = initialCategory;
}
loadTopics("topics", topicFilter, topicStatus);
loadTopics("collections", collectionFilter, collectionStatus);
pollGutenberg();
loadGutenbergTags();
if (initialQuery || initialSource !== "archive" || hasArchiveFilters()) {
    search(Number.isInteger(initialPage) && initialPage > 0 ? initialPage : 1);
}
sourceInput.addEventListener("change", () => {
    updateSourceFields();
    search(1);
});
categoryInput.addEventListener("change", () => search(1));
contentTypeInput.addEventListener("change", () => {
    if (queryInput.value.trim() || hasArchiveFilters()) search(1);
});
languageInput.addEventListener("change", () => {
    languageSearch.value = "";
    languageSearch.dispatchEvent(new Event("input"));
    updateSourceFields();
    search(1);
});
previousButton.addEventListener("click", () => search(currentPage - 1));
nextButton.addEventListener("click", () => search(currentPage + 1));
