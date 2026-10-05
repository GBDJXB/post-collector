"use strict";

// 归档内容只通过 textContent 插入，所有写操作由同源 API 完成。
const $ = (id) => document.getElementById(id);
const lines = (value) => value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
function safeId(value) {
    const id = Number(value);
    if (!Number.isSafeInteger(id) || id <= 0) throw new Error("ID 必须为浏览器能够精确表示的正整数");
    return id;
}
let csrf = "", currentTarget = null, searchParams = null, result = null, busy = false;

async function api(path, body) {
    const response = await fetch(`/api/v1/${path}`, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin",
        headers: body === undefined ? {} : {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
        if (response.status === 401) {
            $("workspace").hidden = true;
            $("session-status").textContent = "会话无效，请使用 pc serve 终端中的授权链接；已用或过期则重启服务。";
        }
        const detail = typeof data.detail === "string" ? data.detail : "参数无效，请检查输入与目标 ID";
        throw new Error(detail + (data.copied ? "；剪贴板复制已完成" : ""));
    }
    return data;
}

async function perform(operation) {
    if (busy) return;
    busy = true;
    $("notice").textContent = "正在处理……";
    const buttons = Array.from(document.querySelectorAll("button"));
    const disabled = buttons.map(b => b.disabled);
    buttons.forEach(b => { b.disabled = true; });
    try {
        await operation();
        $("notice").textContent = "操作完成";
    } catch (error) {
        $("notice").textContent = error.message;
    } finally {
        buttons.forEach((b, i) => { b.disabled = disabled[i]; });
        $("next").disabled = !result?.has_more;
        busy = false;
    }
}

function cell(row, value, className) {
    const td = document.createElement("td");
    td.textContent = value ?? "";
    if (className) td.className = className;
    row.append(td);
    return td;
}

function checkbox(parent, kind, id, label) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.kind = kind;
    input.value = String(safeId(id));
    input.setAttribute("aria-label", label);
    parent.append(input);
    return input;
}

function selected(kind) {
    return Array.from(document.querySelectorAll(`input[data-kind="${kind}"]:checked`), x => safeId(x.value));
}

function targets() {
    const kind = $("tag-target").value;
    const ids = selected(kind);
    if (!ids.length) throw new Error("请勾选标签操作目标");
    return {[kind === "messages" ? "message_ids" : "content_ids"]: ids};
}

function query(values) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(values)) {
        if (Array.isArray(value)) value.forEach(item => params.append(key, item));
        else if (value !== null && value !== "") params.set(key, value);
    }
    return params;
}

async function find(next = false) {
    if (!next) {
        const form = new FormData($("search-form"));
        searchParams = query({
            q: form.get("q"), source: form.get("source"), tags: lines(form.get("tags")),
            item_types: form.getAll("item_types"), since: form.get("since"), until: form.get("until"),
            time_field: form.get("time_field"), sort: form.get("sort"), limit: form.get("limit"),
            include_duplicates: form.has("include_duplicates"), page: 1,
        });
    } else {
        if (!result?.has_more) return;
        searchParams.set("page", result.page + 1);
        if (result.next_cursor) searchParams.set("cursor", result.next_cursor);
    }
    result = await api(`search?${searchParams}`);
    $("hits").replaceChildren();
    for (const hit of result.hits) {
        const row = document.createElement("tr");
        checkbox(cell(row, ""), "messages", hit.message_id, `选择消息 ${hit.message_id}`);
        cell(row, hit.message_id); cell(row, hit.item_type);
        cell(row, (hit.snippet || hit.text || hit.original_name || "").slice(0, 180), "summary");
        cell(row, [...new Set(hit.tags.map(tag => typeof tag === "string" ? tag : tag.name))].join(" "));
        cell(row, hit.source_name || hit.source || hit.source_id);
        cell(row, hit.first_seen_at);
        const button = document.createElement("button");
        button.type = "button"; button.textContent = "查看";
        button.addEventListener("click", () => perform(() => show({message_id: hit.message_id})));
        cell(row, "").append(button);
        $("hits").append(row);
    }
    $("search-summary").textContent = `第 ${result.page} 页，本页 ${result.hits.length} 条；总数 ${result.total ?? "未统计"} (${result.total_kind})${result.incomplete ? "；候选已截断" : ""}`;
}

async function show(target) {
    const data = await api(`detail?${query(target)}`);
    currentTarget = target;
    $("detail-summary").textContent = `消息 ${data.message_id}；来源 ${data.source}；${data.origin_note || ""}`;
    $("parts").replaceChildren();
    for (const part of data.parts) {
        const article = document.createElement("article");
        const label = document.createElement("label");
        checkbox(label, "parts", part.content_id, `选择内容项 ${part.content_id}`);
        label.append(document.createTextNode(`内容项 ${part.content_id} (${part.item_type}) ${part.original_name || ""}`));
        article.append(label);
        if (part.text) { const pre = document.createElement("pre"); pre.textContent = part.text; article.append(pre); }
        for (const media of part.media) {
            const url = `/api/v1/media/${part.content_id}/${media.media_id}`;
            const link = document.createElement("a"); link.href = url;
            link.textContent = `媒体 ${media.media_id} (${media.mime_detected})`; link.target = "_blank"; link.rel = "noopener";
            article.append(link);
            if (["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"].includes(media.mime_detected)) {
                const img = document.createElement("img"); img.src = url; img.alt = `归档图片 ${media.media_id}`; img.loading = "lazy";
                img.addEventListener("error", () => { img.hidden = true; link.textContent += "；读取失败或浏览器无法展示"; });
                article.append(img);
            } else article.append(document.createTextNode("；浏览器预览暂不支持此格式"));
        }
        $("parts").append(article);
    }
}

function table(id, rows, keys) {
    $(id).replaceChildren();
    for (const entry of rows) {
        const row = document.createElement("tr");
        keys.forEach(key => cell(row, Array.isArray(entry[key]) ? entry[key].join(" ") : entry[key]));
        $(id).append(row);
    }
}

async function tags() {
    const data = await api("tags");
    table("tags", data.tags, ["tag_id", "name", "namespace", "status", "usage_count", "aliases"]);
}

async function assignments() {
    const data = await api(`assignments?${query(targets())}`);
    $("assignments").replaceChildren();
    for (const entry of data.assignments) {
        const row = document.createElement("tr");
        const td = cell(row, "");
        if (entry.source === "suggested" && !entry.revoked_at) checkbox(td, "suggestions", entry.assign_id, `选择建议 ${entry.assign_id}`);
        ["assign_id", "content_id", "name", "source", "confidence", "revoked_at"].forEach(key => cell(row, entry[key]));
        $("assignments").append(row);
    }
}

async function refreshContent() {
    await tags();
    if (currentTarget) await show(currentTarget);
    await find();
    $("assignments").replaceChildren();
}

$("search-form").addEventListener("submit", e => { e.preventDefault(); perform(() => find()); });
$("next").addEventListener("click", () => perform(() => find(true)));
$("detail-form").addEventListener("submit", e => {
    e.preventDefault();
    perform(() => show({[$("detail-kind").value]: safeId($("detail-id").value)}));
});
document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => perform(async () => {
    let target = currentTarget;
    if ($("action-target").value === "parts") {
        const ids = selected("parts");
        if (ids.length !== 1) throw new Error("系统操作需要勾选一个内容项");
        target = {content_id: ids[0]};
    }
    if (!target) throw new Error("请先查看目标详情");
    await api(`actions/${button.dataset.action}`, {...target, copy: $("copy-also").checked});
})));
for (const operation of ["add", "remove"]) {
    $(`tag-${operation}`).addEventListener("click", () => perform(async () => {
        await api(`tags/${operation}`, {...targets(), names: lines($("tag-names").value), namespace: $("namespace").value});
        await refreshContent();
    }));
    $(`alias-${operation}`).addEventListener("click", () => perform(async () => {
        await api(`aliases/${operation}`, {alias: $("alias-value").value, name: $("alias-name").value || null});
        await tags();
        if ($("alias-name").value.trim()) {
            const data = await api(`aliases?${query({name: $("alias-name").value})}`);
            table("aliases", data.aliases, ["alias", "name", "namespace"]);
        } else $("aliases").replaceChildren();
    }));
}
for (const operation of ["confirm", "reject"]) $(operation).addEventListener("click", () => perform(async () => {
    const ids = selected("suggestions");
    if (!ids.length) throw new Error("请勾选有效建议关联");
    await api(`suggestions/${operation}`, {ids});
    await refreshContent();
}));
$("tags-load").addEventListener("click", () => perform(tags));
$("assignments-load").addEventListener("click", () => perform(assignments));
$("aliases-load").addEventListener("click", () => perform(async () => {
    const data = await api(`aliases?${query({name: $("alias-name").value})}`);
    table("aliases", data.aliases, ["alias", "name", "namespace"]);
}));
$("logout").addEventListener("click", () => perform(async () => {
    await api("logout", {}); csrf = ""; $("workspace").hidden = true;
    $("session-status").textContent = "已退出。重启 pc serve 可以获取新的授权链接。";
}));

perform(async () => {
    const token = new URLSearchParams(location.hash.slice(1)).get("token");
    history.replaceState(null, "", location.pathname);
    const session = token ? await api("session", {token}) : await api("session");
    csrf = session.csrf;
    $("session-status").textContent = "本机临时 root；会话最长 8 小时，空闲 30 分钟失效。";
    $("workspace").hidden = false;
    await find(); await tags();
});
