import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router";
import { ModelPage, proposalProofs } from "./model.jsx";
import { CompositionsPage } from "./compositions.jsx";
import { CompositionDetailPage } from "./composition_detail.jsx";
import { LinkedText, safeWebUrl } from "./links.jsx";
import { HomePage } from "./home.jsx";
import {
  compactText, EmptyState, formatDate, Icon, PageHeader, statusLabel,
  TagList, TypeMark,
} from "./ui.jsx";

const lines = value => value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
const emptyDraft = { text: "", note: "", images: [], entries: [], orderTouched: false, createComposition: false, compositionName: "", compositionDescription: "", compositionTags: "" };
const initialSearch = { q: "", kind: "all", source: "", tags: "", item_types: [], since: "", until: "", time_field: "ingest", sort: "time", limit: 40, include_duplicates: false };

const navigation = [
  { to: "/", label: "工作台", icon: "home", end: true, hint: "入口" },
  { to: "/add", label: "加入新内容", icon: "plus", hint: "收集" },
  { to: "/manage", label: "检索 / 管理已有内容", icon: "search", hint: "归档" },
  { to: "/compositions", label: "整理组合", icon: "layers", hint: "阅读路径" },
  { to: "/model", label: "模型标签建议", icon: "sparkles", hint: "辅助" },
];

export function safeId(value) {
  const id = Number(value);
  if (!Number.isSafeInteger(id) || id <= 0) throw new Error("ID 必须为浏览器能够精确表示的正整数");
  return id;
}

function query(values) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (Array.isArray(value)) value.forEach(item => params.append(key, item));
    else if (value !== null && value !== undefined && value !== "") params.set(key, value);
  }
  return params;
}

function Selection({ ids, setIds, id, label }) {
  return <input className="selection-check" type="checkbox" aria-label={label} checked={ids.includes(id)} onChange={e => setIds(e.target.checked ? [...ids, safeId(id)] : ids.filter(x => x !== id))} />;
}

function Table({ headers, rows, columns }) {
  return <div className="table-wrap compact-table"><table><thead><tr>{headers.map(h => <th key={h}>{h}</th>)}</tr></thead>
    <tbody>{rows.map((row, index) => <tr key={index}>{columns.map(key => <td key={key}>{Array.isArray(row[key]) ? row[key].join(" ") : row[key] ?? ""}</td>)}</tr>)}</tbody></table></div>;
}

function currentPage(pathname) {
  if (pathname.startsWith("/compositions/")) return { eyebrow: "COMPOSITION EDITOR", title: "组合编辑台" };
  return navigation.find(item => item.end ? pathname === item.to : pathname.startsWith(item.to)) || navigation[0];
}

export function Shell() {
  const location = useLocation();
  const [csrf, setCsrf] = useState("");
  const [session, setSession] = useState("正在检查本机会话……");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const started = useRef(false);
  const csrfRef = useRef("");
  const [draft, setDraft] = useState(emptyDraft);
  const [upload, setUpload] = useState(null);
  const [fileKey, setFileKey] = useState(0);
  const api = useCallback(async (path, body) => {
    const form = body instanceof FormData;
    const response = await fetch(`/api/v1/${path}`, {
      method: body === undefined ? "GET" : "POST", credentials: "same-origin",
      headers: body === undefined ? {} : { ...(form ? {} : { "Content-Type": "application/json" }), "X-CSRF-Token": csrfRef.current },
      body: body === undefined ? undefined : form ? body : JSON.stringify(body),
    });
    let data, invalidJson = false;
    try { data = await response.json(); }
    catch { invalidJson = true; }
    const payload = data && typeof data === "object" && !Array.isArray(data) ? data : {};
    if (response.status === 401) { setCsrf(""); csrfRef.current = ""; setSession("会话失效，请使用服务终端中的新授权链接。"); }
    if (!response.ok) {
      const detail = invalidJson ? `服务请求失败（HTTP ${response.status}），请检查服务终端`
        : typeof payload.detail === "string" ? payload.detail : "参数无效，请检查输入";
      const error = new Error(detail + (payload.copied ? "；剪贴板复制已完成" : ""));
      error.status = response.status;
      throw error;
    }
    if ((invalidJson && response.status !== 204) || (!invalidJson && (data === null || typeof data !== "object" || Array.isArray(data)))) {
      const error = new Error(`服务响应格式无效（HTTP ${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }, []);
  const run = async operation => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setNotice("正在处理……");
    try { await operation(); setNotice("操作完成"); }
    catch (error) { setNotice(error.message); }
    finally { lock.current = false; setBusy(false); }
  };
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
    if (token) window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);
    api("session", token ? { token } : undefined).then(data => {
      csrfRef.current = data.csrf; setCsrf(data.csrf);
      setSession("本机临时 root；会话最长 8 小时，空闲 30 分钟失效。");
    }).catch(error => { if (error.status !== 401) setSession(error.message); });
  }, [api]);
  const manage = useManage(api);
  useEffect(() => {
    if (!notice || notice === "正在处理……") return;
    const timer = window.setTimeout(() => setNotice(""), 6500);
    return () => window.clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (csrf) run(async () => { await manage.find(false); await manage.loadTags(); });
  }, [csrf]);
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const messageId = params.get("message_id");
    const contentId = params.get("content_id");
    const compositionId = params.get("composition_id");
    if (!csrf || location.pathname !== "/manage") return;
    try {
      if (messageId) manage.show({ message_id: safeId(messageId) }).catch(error => setNotice(error.message));
      else if (contentId) manage.show({ content_id: safeId(contentId) }).catch(error => setNotice(error.message));
      else if (compositionId) manage.showComposition(safeId(compositionId)).catch(error => setNotice(error.message));
    } catch (error) { setNotice(error.message); }
  }, [csrf, location.pathname, location.search]);
  useEffect(() => {
    const page = currentPage(location.pathname);
    document.title = `${page.label || page.title} · 拾页 post-collector`;
  }, [location.pathname]);
  useEffect(() => {
    const focusSearch = event => {
      if ((event.key === "/" || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k")) && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
        const input = document.querySelector("[data-search-input]");
        if (input) { event.preventDefault(); input.focus(); }
      }
    };
    window.addEventListener("keydown", focusSearch);
    return () => window.removeEventListener("keydown", focusSearch);
  }, [location.pathname]);
  function addEntry(entry) { setDraft(current => ({ ...current, entries: [...current.entries, entry] })); }
  function updateEntry(index, changed) { setDraft(current => ({ ...current, entries: current.entries.map((entry, position) => position === index ? { ...entry, ...changed } : entry) })); }
  function moveEntry(from, to) {
    setDraft(current => { const entries = [...current.entries]; const [entry] = entries.splice(from, 1); entries.splice(to, 0, entry); return { ...current, entries, orderTouched: true }; });
  }
  async function submit() {
    const orderMode = draft.orderTouched || draft.entries.some(entry => entry.kind !== "image");
    const images = orderMode ? draft.entries.filter(entry => entry.kind === "image").map(entry => entry.file) : draft.images;
    if (!draft.text.trim() && !images.length && !draft.entries.some(entry => entry.kind === "text" && entry.text.trim() || entry.kind === "link" && entry.url.trim())) throw new Error("请输入文本、链接或选择图片");
    if (images.length > 19) throw new Error("一次最多上传 19 张图片");
    const body = new FormData();
    if (orderMode) {
      const entries = draft.text.trim() ? [{ kind: "text", text: draft.text }, ...draft.entries] : draft.entries;
      let imageIndex = 0;
      body.append("parts", JSON.stringify(entries.map(entry => entry.kind === "image" ? { kind: "image", index: imageIndex++ } : entry)));
    } else body.append("text", draft.text);
    body.append("note", draft.note);
    images.forEach(file => body.append("images", file));
    if (draft.createComposition) {
      body.append("create_composition", "true"); body.append("composition_name", draft.compositionName);
      body.append("composition_description", draft.compositionDescription);
      body.append("composition_tags", draft.compositionTags);
    }
    const result = await api("uploads", body);
    setUpload(result);
    setDraft(emptyDraft); setFileKey(x => x + 1);
    await manage.find(false);
  }
  const page = currentPage(location.pathname);
  const today = new Intl.DateTimeFormat("zh-CN", { weekday: "long", month: "long", day: "numeric" }).format(new Date());

  return <div className={`app-shell${csrf ? " is-ready" : " is-booting"}`}>
    <aside className="sidebar">
      <Link to="/" className="brand" aria-label="拾页 post-collector 工作台">
        <span className="brand-mark">拾</span>
        <span className="brand-copy"><strong>拾页</strong><small>POST COLLECTOR</small></span>
      </Link>
      <Link to="/add" className="sidebar-add"><span><Icon name="plus" size={17} /></span><span><strong>收集一页</strong><small>文字 · 图片 · 链接</small></span></Link>
      <nav className="sidebar-nav" aria-label="主导航">
        <p className="nav-label">LIBRARY</p>
        {navigation.map((item, index) => <NavLink key={item.to} to={item.to} end={item.end} aria-label={item.label} className={({ isActive }) => `nav-item${isActive ? " is-active" : ""}`}>
          <span className="nav-icon"><Icon name={item.icon} size={17} /></span>
          <span className="nav-copy"><strong>{item.label}</strong><small>{item.hint}</small></span>
          <span className="nav-index">{String(index + 1).padStart(2, "0")}</span>
        </NavLink>)}
      </nav>
      <div className="sidebar-foot">
        <div className="session-card">
          <span className={`status-dot${csrf ? " is-live" : ""}`} />
          <div><strong>{csrf ? "本机会话在线" : "等待本机授权"}</strong><p id="session-status">{session}</p></div>
        </div>
        {csrf && <button className="sidebar-logout" disabled={busy} onClick={() => run(async () => { await api("logout", {}); setCsrf(""); csrfRef.current = ""; setSession("已退出，请重启服务获取新授权链接。"); })}><Icon name="logout" size={15} />退出本机会话</button>}
        <p className="shortcut-note"><kbd>Ctrl</kbd><span>+</span><kbd>K</kbd> 聚焦检索</p>
      </div>
    </aside>

    {csrf && <main className="app-main">
      <div className="topbar">
        <div className="topbar-route"><span>{today}</span><i /><span>{page.hint || page.eyebrow || "拾页"}</span></div>
        <div className="topbar-tools">
          <span className="local-badge"><span className="status-dot is-live" />仅本机</span>
          <Link to="/manage" className="topbar-search"><Icon name="search" size={15} />检索归档<kbd>⌘K</kbd></Link>
        </div>
      </div>
      <p id="notice" role="status" aria-live="polite" className={`notice${notice ? " is-visible" : ""}${/[；:：]|失败|无效|错误|超过|请|不可用|会话/.test(notice) ? " is-error" : ""}`}>{notice}</p>
      <fieldset className="workspace" disabled={busy}>
        <Routes>
          <Route path="/" element={<HomePage api={api} />} />
          <Route path="/add" element={<AddPage draft={draft} setDraft={setDraft} addEntry={addEntry} updateEntry={updateEntry} moveEntry={moveEntry} fileKey={fileKey} submit={submit} run={run} upload={upload} />} />
          <Route path="/manage" element={<Manage state={manage} run={run} api={api} />} />
          <Route path="/compositions" element={<CompositionsPage api={api} run={run} parseId={safeId} />} />
          <Route path="/compositions/:composition_id" element={<CompositionDetailPage api={api} run={run} parseId={safeId} />} />
          <Route path="/model" element={<ModelPage api={api} run={run} parseId={safeId} />} />
          <Route path="*" element={<div className="page"><EmptyState icon="warning" title="页面不存在" action={<Link className="button button-primary" to="/">返回工作台</Link>}>这个地址没有对应的归档页面。</EmptyState></div>} />
        </Routes>
      </fieldset>
    </main>}
  </div>;
}

function AddPage({ draft, setDraft, addEntry, updateEntry, moveEntry, fileKey, submit, run, upload }) {
  const displayImages = draft.orderTouched ? draft.entries.filter(entry => entry.kind === "image").map(entry => entry.file) : draft.images;
  const partsCount = (draft.text.trim() ? 1 : 0) + draft.entries.length;
  return <div className="page add-page">
    <PageHeader eyebrow="NEW ENTRY / 新收集" title="加入新内容" description="写下一段文字、放下几张图片，或保存一个链接。它们会作为同一条消息按顺序归档，之后可以再被打标签或编入组合。" actions={<Link className="button button-ghost" to="/manage"><Icon name="library" size={16} />查看归档</Link>} />
    <form className="composer-layout" onSubmit={e => { e.preventDefault(); run(submit); }}>
      <section className="surface composer-main">
        <div className="composer-toolbar">
          <div className="composer-label"><span className="composer-dot" /><span>一条新的归档消息</span></div>
          <span className="composer-count">{partsCount ? `${partsCount} 个部件` : "等待内容"}</span>
        </div>

        <label className="field field-body">
          <span className="field-label">文本</span>
          <textarea name="text" rows="8" value={draft.text} onChange={e => setDraft({ ...draft, text: e.target.value })} placeholder="写下想保存的文字。这里的内容会保持换行，并在检索中按关键词出现。" />
        </label>

        <div className="media-drop-row">
          <label className="file-drop" htmlFor="composer-images">
            <input id="composer-images" key={fileKey} name="images" type="file" multiple accept=".png,.jpg,.jpeg,.gif,.webp,.avif,.heic,.heif" aria-label="图片" onChange={e => { const files = Array.from(e.target.files); setDraft(current => ({ ...current, images: [...current.images, ...files], entries: [...current.entries, ...files.map(file => ({ kind: "image", file }))] })); }} />
            <span className="file-drop-icon"><Icon name="image" size={22} /></span>
            <span className="file-drop-copy"><strong>添加图片</strong><small>PNG / JPEG / GIF / WebP / AVIF / HEIC，最多 19 张</small></span>
            <span className="file-drop-action">选择文件</span>
          </label>
          <div className="add-part-actions">
            <button type="button" className="button button-ghost" onClick={() => addEntry({ kind: "text", text: "" })}><Icon name="text" size={15} />添加文本条目</button>
            <button type="button" className="button button-ghost" onClick={() => addEntry({ kind: "link", url: "", description: "" })}><Icon name="link" size={15} />添加链接条目</button>
          </div>
        </div>

        {displayImages.length > 0 && <ul className="file-preview-list">{displayImages.map((file, index) => <li key={`${file.name}-${index}`}><span className="file-chip-icon"><Icon name="image" size={14} /></span><span>{file.name} ({file.size} 字节)</span></li>)}</ul>}

        {draft.entries.length > 0 && <div className="ordered-editor">
          <div className="ordered-editor-heading"><div><p className="eyebrow">ORDERED PARTS</p><h3>消息中的条目顺序</h3></div><span>图片也会按此顺序保存</span></div>
          <ol className="entry-list">
            {draft.entries.map((entry, index) => <li key={index} className={`entry-item entry-${entry.kind}`}>
              <span className="entry-handle"><Icon name="drag" size={16} /></span>
              <span className="entry-number">{String(index + 1).padStart(2, "0")}</span>
              <div className="entry-body">
                {entry.kind === "text" && <label className="field"><span className="field-label">条目 {index + 1} 文本</span><textarea value={entry.text} onChange={e => updateEntry(index, { text: e.target.value })} placeholder="补充一段正文…" /></label>}
                {entry.kind === "link" && <div className="entry-link-fields"><label className="field"><span className="field-label">条目 {index + 1} URL</span><input value={entry.url} onChange={e => updateEntry(index, { url: e.target.value })} placeholder="https://…" /></label><label className="field"><span className="field-label">条目 {index + 1} 说明</span><textarea rows="2" value={entry.description} onChange={e => updateEntry(index, { description: e.target.value })} placeholder="为什么保存这个链接？" /></label></div>}
                {entry.kind === "image" && <div className="entry-image"><span className="file-chip-icon"><Icon name="image" size={15} /></span><span>图片 {entry.file.name}</span><small>{entry.file.size} 字节</small></div>}
              </div>
              <div className="entry-actions">
                <button type="button" className="icon-button" aria-label={`上移条目 ${index + 1}`} disabled={index === 0} onClick={() => moveEntry(index, index - 1)}><Icon name="arrowUp" size={15} /></button>
                <button type="button" className="icon-button" aria-label={`下移条目 ${index + 1}`} disabled={index === draft.entries.length - 1} onClick={() => moveEntry(index, index + 1)}><Icon name="arrowDown" size={15} /></button>
                <button type="button" className="icon-button is-danger" aria-label={`移除条目 ${index + 1}`} onClick={() => setDraft(current => ({ ...current, entries: current.entries.filter((_, position) => position !== index), orderTouched: true }))}><Icon name="close" size={15} /></button>
              </div>
            </li>)}
          </ol>
        </div>}

        <div className="composer-footnote"><Icon name="archive" size={15} /><span>一次提交组成一条消息。提交后会自动扫描、去重并入库。</span></div>
      </section>

      <aside className="composer-aside">
        <section className="surface side-card">
          <div className="section-heading"><div><p className="eyebrow">ARCHIVE NOTES</p><h2>归档设置</h2></div></div>
          <label className="field"><span className="field-label">备注</span><textarea aria-label="备注" name="note" rows="3" maxLength="4096" value={draft.note} onChange={e => setDraft({ ...draft, note: e.target.value })} placeholder="给自己留一句上下文，不会替代正文。" /><span className="field-help">最多 4096 字符，仅作为消息注解。</span></label>
          <label className="switch-row"><span><strong>同时创建组合</strong><small>把这次加入的内容排成一个新的阅读路径</small></span><input type="checkbox" checked={draft.createComposition} onChange={e => setDraft({ ...draft, createComposition: e.target.checked })} /></label>
          {draft.createComposition && <div className="composition-create-fields">
            <label className="field"><span className="field-label">上传组合名称</span><input value={draft.compositionName} maxLength="255" onChange={e => setDraft({ ...draft, compositionName: e.target.value })} placeholder="例如：周末读完的几篇" /></label>
            <label className="field"><span className="field-label">上传组合说明</span><textarea value={draft.compositionDescription} onChange={e => setDraft({ ...draft, compositionDescription: e.target.value })} placeholder="这条阅读路径为什么成立？" /></label>
            <label className="field"><span className="field-label">上传组合标签（每行一个）</span><textarea aria-label="组合标签（每行一个）" value={draft.compositionTags} onChange={e => setDraft({ ...draft, compositionTags: e.target.value })} placeholder={"慢读\n设计"} /><span className="field-help">组合标签独立保存，不会复制给成员。</span></label>
          </div>}
        </section>
        <section className="surface side-card submission-card">
          <p className="eyebrow">READY TO FILE</p>
          <h2>{partsCount ? "准备归档" : "还没有内容"}</h2>
          <dl className="mini-stats"><div><dt>部件</dt><dd>{partsCount}</dd></div><div><dt>图片</dt><dd>{displayImages.length}</dd></div><div><dt>模式</dt><dd>{draft.createComposition ? "含组合" : "仅消息"}</dd></div></dl>
          <button type="submit" className="button button-primary button-wide"><Icon name="plus" size={16} />加入内容</button>
          <p className="safe-note"><Icon name="shield" size={14} />内容只写入本机归档；不会抓取链接网页。</p>
        </section>
        {upload && <section className="surface upload-result">
          <div className="upload-result-head"><span className={`status-dot${upload.status === "succeeded" ? " is-live" : ""}`} /><strong>最近一次提交</strong></div>
          <p>消息 {upload.message_id}：{upload.duplicate ? "已存在；" : ""}{upload.detail} ({statusLabel(upload.status)})</p>
          {upload.content_ids?.length > 0 && <Link to={`/manage?message_id=${upload.message_id}`} className="text-link">查看本次消息 <Icon name="arrowRight" size={14} /></Link>}
          {upload.composition_id && <Link to={`/compositions/${upload.composition_id}`} className="text-link">打开新组合 <Icon name="arrowRight" size={14} /></Link>}
        </section>}
      </aside>
    </form>
  </div>;
}

function useManage(api) {
  const [form, setForm] = useState(initialSearch), [result, setResult] = useState(null);
  const [params, setParams] = useState(null), [detail, setDetail] = useState(null), [target, setTarget] = useState(null);
  const [messages, setMessages] = useState([]), [parts, setParts] = useState([]), [suggestions, setSuggestions] = useState([]);
  const [tags, setTags] = useState([]), [assignments, setAssignments] = useState([]), [aliases, setAliases] = useState([]);
  const [fields, setFields] = useState({ detailKind: "message_id", detailId: "", actionTarget: "current", copy: false, tagTarget: "messages", names: "", namespace: "general", aliasName: "", aliasValue: "" });
  const [compositionDetail, setCompositionDetail] = useState(null);
  async function find(next = false) {
    const values = next ? { ...params, page: result.page + 1, cursor: result.next_cursor || null } : { ...form, tags: lines(form.tags), page: 1 };
    const data = await api(`search?${query({ ...values, result_version: 2 })}`); setResult(data); setParams(values); setMessages([]); setParts([]);
    if (data.result_version === 2) setFields(current => ({ ...current, tagTarget: "parts" }));
  }
  async function show(value) { const data = await api(`detail?${query(value)}`); setDetail(data); setCompositionDetail(null); setTarget(value); setParts([]); }
  async function showComposition(id) { const data = await api(`compositions/${id}`); setCompositionDetail(data); setDetail(null); setTarget(null); setParts([]); }
  async function loadTags() { setTags((await api("tags")).tags); }
  function targets() {
    const ids = fields.tagTarget === "messages" ? messages : parts;
    if (!ids.length) throw new Error("请勾选标签操作目标");
    return { [fields.tagTarget === "messages" ? "message_ids" : "content_ids"]: ids };
  }
  async function loadAssignments() { setAssignments((await api(`assignments?${query(targets())}`)).assignments); setSuggestions([]); }
  async function loadAliases() { setAliases((await api(`aliases?${query({ name: fields.aliasName })}`)).aliases); }
  async function refresh() { await loadTags(); if (target) await show(target); await find(); setAssignments([]); setSuggestions([]); }
  return { form, setForm, result, detail, compositionDetail, target, messages, setMessages, parts, setParts, suggestions, setSuggestions, tags, assignments, aliases, setAliases, fields, setFields, find, show, showComposition, loadTags, targets, loadAssignments, loadAliases, refresh };
}

function Manage({ state: m, run, api }) {
  const [activeIndex, setActiveIndex] = useState(-1);
  const field = (key, label, type = "text", props = {}) => <label className="field"><span className="field-label">{label}</span><input type={type} value={m.fields[key]} onChange={e => m.setFields({ ...m.fields, [key]: e.target.value })} {...props} /></label>;
  const select = (key, label, choices) => <label className="field"><span className="field-label">{label}</span><select value={m.fields[key]} onChange={e => m.setFields({ ...m.fields, [key]: e.target.value })}>{choices.map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>;
  const searchField = (key, label, type = "text", props = {}) => <label className="field"><span className="field-label">{label}</span><input name={key} type={type} value={m.form[key]} onChange={e => m.setForm({ ...m.form, [key]: e.target.value })} {...props} /></label>;
  async function action(kind) {
    let target = m.target;
    if (m.fields.actionTarget === "parts") {
      if (m.parts.length !== 1) throw new Error("系统操作需要勾选一个内容项");
      target = { content_id: m.parts[0] };
    }
    if (!target) throw new Error("请先查看目标详情");
    await api(`actions/${kind}`, { ...target, copy: m.fields.copy });
  }
  async function tag(operation) { await api(`tags/${operation}`, { ...m.targets(), names: lines(m.fields.names), namespace: m.fields.namespace }); await m.refresh(); }
  async function alias(operation) {
    await api(`aliases/${operation}`, { alias: m.fields.aliasValue, name: m.fields.aliasName || null });
    await m.loadTags(); if (m.fields.aliasName.trim()) await m.loadAliases(); else m.setAliases([]);
  }
  async function review(operation) {
    if (!m.suggestions.length) throw new Error("请勾选有效建议关联");
    await api(`suggestions/${operation}`, { ids: m.suggestions, expected_runs: proposalProofs(m.assignments, m.suggestions) }); await m.refresh();
  }
  function openHit(hit, index) {
    setActiveIndex(index);
    return hit.kind === "composition" ? m.showComposition(hit.composition_id) : m.show(hit.kind === "content" ? { content_id: hit.content_id } : { message_id: hit.message_id });
  }
  function stepHit(delta) {
    const hits = m.result?.hits || [];
    const next = activeIndex + delta;
    if (hits[next]) run(() => openHit(hits[next], next));
  }
  const hits = m.result?.hits || [];
  const isV2 = m.result?.result_version === 2;

  return <div className="page manage-page">
    <PageHeader eyebrow="LIBRARY / 内容归档" title="检索、阅读与标注" description="内容项与组合在同一条时间线上出现。关键词可以命中组合名称、说明，也可以命中组合中的可见成员。" actions={<Link className="button button-ghost" to="/compositions"><Icon name="layers" size={16} />整理已有组合</Link>} />
    <div className="manage-viewport" role="region" aria-label="检索与详情" tabIndex={0}>
      <div className="manage-layout">
        <section className="surface search-panel">
          <div className="section-heading search-heading"><div><p className="eyebrow">SEARCH THE ARCHIVE</p><h2>检索内容项与组合</h2></div><span className="result-mode">{isV2 ? "平级结果 · 时间排序" : "消息结果"}</span></div>
          <form id="search-form" className="search-form" onSubmit={e => { e.preventDefault(); setActiveIndex(-1); run(() => m.find()); }}>
            <div className="search-primary-row">
              <label className="search-input"><Icon name="search" size={18} /><span className="sr-only">关键词</span><input data-search-input aria-label="关键词" name="q" type="text" maxLength="4096" value={m.form.q} onChange={e => m.setForm({ ...m.form, q: e.target.value })} placeholder="输入关键词，搜索文字、组合或成员…" /><kbd>⌘K</kbd></label>
              <label className="compact-select"><span className="sr-only">结果类型</span><select aria-label="结果类型" value={m.form.kind} onChange={e => m.setForm({ ...m.form, kind: e.target.value })}><option value="all">内容项与组合</option><option value="content">仅内容项</option><option value="composition">仅组合</option></select></label>
              <button className="button button-primary" type="submit">检索 / 返回第一页</button>
            </div>
            <div className="filter-grid">
              {searchField("source", "来源名或 ID")}
              <label className="field filter-tags"><span className="field-label">标签（每行一个，全部匹配）</span><textarea name="tags" rows="1" value={m.form.tags} onChange={e => m.setForm({ ...m.form, tags: e.target.value })} placeholder={"例如：设计\n待读"} /></label>
              {searchField("since", "开始日期", "date")}
              {searchField("until", "结束日期", "date")}
              <label className="field"><span className="field-label">时间字段</span><select name="time_field" value={m.form.time_field} onChange={e => m.setForm({ ...m.form, time_field: e.target.value })}><option value="ingest">入库时间</option><option value="sent">发送时间</option></select></label>
              <label className="field"><span className="field-label">排序</span><select name="sort" value={m.form.sort} onChange={e => m.setForm({ ...m.form, sort: e.target.value })}><option value="time">时间</option></select></label>
              {searchField("limit", "每页", "number", { min: 1, max: 50 })}
            </div>
            <fieldset className="type-filter"><legend>内容类型（不勾选表示全部）</legend>{[["text", "文本"], ["image", "图片"], ["mixed", "混合组合（成员含多种类型）"], ["video", "视频"], ["audio", "音频"], ["link", "链接"], ["sticker", "贴纸"], ["file", "文件"]].map(([value, label]) => <label key={value} className={`filter-chip${m.form.item_types.includes(value) ? " is-selected" : ""}`}><input type="checkbox" name="item_types" checked={m.form.item_types.includes(value)} onChange={e => m.setForm({ ...m.form, item_types: e.target.checked ? [...m.form.item_types, value] : m.form.item_types.filter(x => x !== value) })} />{label}</label>)}</fieldset>
          </form>
          {m.result && <div className="result-toolbar"><p id="search-summary">第 {m.result.page} 页，本页 {hits.length} 条；总数 {m.result.total ?? "未统计"} ({m.result.total_kind || "estimate"}){m.result.incomplete ? "；候选已截断" : ""}</p><span>{isV2 ? "内容项按实际条目展示，不折叠重复" : "按消息展示"}</span></div>}
          <div className="table-wrap result-table-wrap">
            <table className="result-table">
              <thead><tr>{["选择", isV2 ? "结果 ID" : "消息 ID", "类型", "内容", "标签", "来源", "时间", "详情"].map(x => <th key={x}>{x}</th>)}</tr></thead>
              <tbody id="hits">
                {isV2 ? hits.map((hit, index) => <tr key={`${hit.kind}:${hit.entity_id}`} className={`hit-row hit-${hit.kind}${activeIndex === index ? " is-active" : ""}`} data-type={hit.kind === "composition" ? "composition" : hit.item_type}>
                  <td className="hit-select">{hit.kind === "content" && <Selection ids={m.parts} setIds={m.setParts} id={hit.content_id} label={`选择内容项 ${hit.content_id}`} />}</td>
                  <td className="hit-ref">{hit.kind === "composition" ? <><span className="hit-kind">组合</span><strong>#{hit.composition_id}</strong></> : <><span className="hit-kind">内容项</span><strong>#{hit.content_id}</strong></>}</td>
                  <td className="hit-type"><TypeMark kind={hit.kind} type={hit.item_type} compact /></td>
                  <td className="summary"><span className="summary-clamp">{hit.kind === "composition" ? hit.name : (hit.text || hit.original_name || "").slice(0, 180)}</span>{hit.kind === "composition" && hit.description && <small className="summary-sub">{compactText(hit.description, 90)}</small>}</td>
                  <td className="hit-tags"><TagList tags={hit.tags || []} limit={4} empty={<span className="muted">无标签</span>} /></td>
                  <td className="hit-source">{hit.source_name || (hit.kind === "composition" ? "组合" : "—")}</td>
                  <td className="hit-time"><span>{formatDate(hit.sort_time, true)}</span></td>
                  <td className="hit-actions"><button className="button button-small button-ghost" aria-label={hit.kind === "composition" ? `查看组合 ${hit.composition_id}` : `查看内容项 ${hit.content_id}`} onClick={() => run(() => openHit(hit, index))}><Icon name="eye" size={14} /><span>查看</span></button>{hit.kind === "composition" && <Link className="quiet-link hit-manage-link" to={`/compositions/${hit.composition_id}`}>管理组合 {hit.composition_id}</Link>}</td>
                </tr>) : hits.map((hit, index) => <tr key={hit.message_id} className={`hit-row hit-message${activeIndex === index ? " is-active" : ""}`} data-type={hit.item_type}>
                  <td className="hit-select"><Selection ids={m.messages} setIds={m.setMessages} id={hit.message_id} label={`选择消息 ${hit.message_id}`} /></td>
                  <td className="hit-ref"><span className="hit-kind">消息</span><strong>#{hit.message_id}</strong></td>
                  <td className="hit-type"><TypeMark type={hit.item_type} compact /></td>
                  <td className="summary"><span className="summary-clamp">{(hit.snippet || hit.text || hit.original_name || "").slice(0, 180)}</span></td>
                  <td className="hit-tags"><TagList tags={(hit.tags || []).map(tag => typeof tag === "string" ? { name: tag } : tag)} limit={4} empty={<span className="muted">无标签</span>} /></td>
                  <td className="hit-source">{hit.source_name || hit.source || hit.source_id || "—"}</td>
                  <td className="hit-time"><span>{formatDate(hit.first_seen_at, true)}</span></td>
                  <td className="hit-actions"><button className="button button-small button-ghost" onClick={() => run(() => { setActiveIndex(index); return m.show({ message_id: hit.message_id }); })}><Icon name="eye" size={14} /><span>查看</span></button></td>
                </tr>)}
              </tbody>
            </table>
            {m.result && !hits.length && <EmptyState icon="search" title="没有找到匹配的内容"><p>换一个关键词，或减少标签和类型筛选。</p></EmptyState>}
          </div>
          <div className="result-footer"><button id="next" className="button button-ghost" disabled={!m.result?.has_more} onClick={() => run(() => m.find(true))}>下一页 <Icon name="arrowRight" size={15} /></button>{m.result?.has_more && <span>还有更多结果</span>}</div>
        </section>

        <section className="surface manage-detail detail-pane">
          <div className="detail-heading"><div><p className="eyebrow">READING DESK</p><h2>详情与本机操作</h2></div>{activeIndex >= 0 && <div className="detail-steps"><button className="icon-button" aria-label="上一条" disabled={activeIndex <= 0} onClick={() => stepHit(-1)}><Icon name="chevronLeft" size={17} /></button><span>{activeIndex + 1} / {hits.length}</span><button className="icon-button" aria-label="下一条" disabled={activeIndex >= hits.length - 1} onClick={() => stepHit(1)}><Icon name="chevronRight" size={17} /></button></div>}</div>
          <details className="id-lookup"><summary><Icon name="hash" size={14} />按 ID 直接定位</summary><form onSubmit={e => { e.preventDefault(); setActiveIndex(-1); run(() => m.show({ [m.fields.detailKind]: safeId(m.fields.detailId) })); }}>
            {select("detailKind", "目标", [["message_id", "消息 ID"], ["content_id", "内容项 ID"]])}{field("detailId", "ID", "number", { min: 1, required: true })}<button className="button button-small button-ghost" type="submit">查看</button></form></details>
          {!m.detail && !m.compositionDetail && <EmptyState icon="bookOpen" title="从一条结果开始阅读"><p>点击左侧结果的“查看”，完整内容会在这里展开。你可以在阅读时继续筛选、翻页和打标签。</p></EmptyState>}
          {m.detail && <article className="reader">
            <header className="reader-header"><div className="reader-kicker"><span className="reader-rule" /><span>消息 {m.detail.message_id}</span><span>·</span><span>{m.detail.source}</span></div><h3>{compactText((m.detail.parts || []).find(part => part.text)?.text, 70) || "无标题内容"}</h3><div className="reader-meta"><span><Icon name="clock" size={13} />{formatDate(m.detail.sent_at || m.detail.parts?.[0]?.first_seen_at, true)}</span>{m.detail.origin_note && <span className="reader-note"><Icon name="pin" size={13} />{m.detail.origin_note}</span>}</div></header>
            <div id="parts" className="reader-parts">{(m.detail.parts || []).map(part => <article className="reader-part" key={part.content_id} data-type={part.item_type}>
              <div className="reader-part-head"><label className="part-select"><Selection ids={m.parts} setIds={m.setParts} id={part.content_id} label={`选择内容项 ${part.content_id}`} /><span>内容项 {part.content_id} ({part.item_type})</span></label>{part.original_name && <span className="reader-file">{part.original_name}</span>}</div>
              {part.url && safeWebUrl(part.url) && <a className="reader-link" href={safeWebUrl(part.url)} target="_blank" rel="noopener noreferrer"><Icon name="link" size={15} />{part.url}<Icon name="external" size={13} /></a>}
              {part.text && <pre><LinkedText text={part.text} /></pre>}
              {(part.media || []).map(media => <Media key={media.media_id} part={part} media={media} />)}
            </article>)}</div>
            <div className="reader-actions">{select("actionTarget", "系统操作目标", [["current", "当前查看目标"], ["parts", "详情中勾选的一个内容项"]])}<label className="checkbox-row"><input type="checkbox" checked={m.fields.copy} onChange={e => m.setFields({ ...m.fields, copy: e.target.checked })} />打开 / 定位时同时复制</label><div className="action-row">{[["open", "默认程序打开", "external"], ["reveal", "文件管理器定位", "archive"], ["copy", "复制", "copy"]].map(([value, label, icon]) => <button className="button button-ghost" key={value} onClick={() => run(() => action(value))}><Icon name={icon} size={15} />{label}</button>)}</div><p className="field-help">文本复制正文；图片复制文件。混合内容打开为 HTML，暂不支持复制。</p></div>
          </article>}
          {m.compositionDetail && <article className="reader composition-reader">
            <header className="reader-header"><div className="reader-kicker"><span className="reader-rule" /><span>独立组合 {m.compositionDetail.composition_id}</span><span>·</span><span>{m.compositionDetail.members.length} 个位置</span></div><h3>{m.compositionDetail.name}</h3>{m.compositionDetail.description && <p className="reader-description">{m.compositionDetail.description}</p>}<div className="reader-meta"><span><Icon name="layers" size={13} />按位置顺序阅读</span><Link className="quiet-link" to={`/compositions/${m.compositionDetail.composition_id}`}>管理组合 {m.compositionDetail.composition_id}</Link></div></header>
            <div className="own-tags-block"><span className="own-tags-label">组合自身标签</span><TagList tags={m.compositionDetail.tags || []} empty={<span className="muted">未添加组合标签</span>} /><small>只属于这个组合，不传播给成员。</small></div>
            <div id="parts" className="reader-parts sequence-parts">{m.compositionDetail.members.map((member, index) => <article className="reader-part sequence-part" key={member.position} data-position={String(index + 1).padStart(2, "0")}>
              {member.available ? <>{`内容项 ${member.content_id} (${member.item_type}) `}{member.text && <pre><LinkedText text={member.text} /></pre>}{member.url && safeWebUrl(member.url) && <a className="reader-link" href={safeWebUrl(member.url)} target="_blank" rel="noopener noreferrer">{member.url}</a>}{(member.media || []).map(media => <Media key={media.media_id} part={member} media={media} />)}</> : `位置 ${member.position}：不可用`}
            </article>)}</div>
          </article>}
        </section>
      </div>
    </div>

    <section className="surface tag-workbench">
      <div className="section-heading"><div><p className="eyebrow">ANNOTATE</p><h2>人工标签与关联</h2></div><p>标签作用于选中的内容项；组合标签在组合编辑页单独维护。</p></div>
      <div className="tag-workbench-grid">
        <div className="tag-controls">
          {select("tagTarget", "标签操作目标", isV2 ? [["parts", "结果或详情中勾选的内容项"]] : [["messages", "结果中勾选的消息"], ["parts", "详情中勾选的内容项"]])}
          <label className="field"><span className="field-label">标签名称（每行一个）</span><textarea rows="3" value={m.fields.names} onChange={e => m.setFields({ ...m.fields, names: e.target.value })} placeholder={"设计\n待读"} /></label>
          {field("namespace", "命名空间")}
          <div className="action-row"><button className="button button-primary" onClick={() => run(() => tag("add"))}><Icon name="tag" size={15} />添加标签</button><button className="button button-ghost" onClick={() => run(() => tag("remove"))}>撤销标签</button><button className="button button-ghost" onClick={() => run(m.loadAssignments)}>查看目标关联记录</button></div>
          <div className="selection-summary"><span>当前选择</span><strong>{m.fields.tagTarget === "messages" ? m.messages.length : m.parts.length}</strong><span>项</span></div>
        </div>
        <div className="association-panel">
          <div className="section-heading mini"><h3>关联记录</h3><span>建议须逐条确认或拒绝</span></div>
          <div className="table-wrap compact-table"><table><thead><tr>{["建议选择", "关联 ID", "内容 ID", "名称", "来源", "置信度", "撤销时间"].map(x => <th key={x}>{x}</th>)}</tr></thead><tbody>{m.assignments.map(entry => <tr key={entry.assign_id}><td>{entry.source === "suggested" && !entry.revoked_at && <Selection ids={m.suggestions} setIds={m.setSuggestions} id={entry.assign_id} label={`选择建议 ${entry.assign_id}`} />}</td>{["assign_id", "content_id", "name", "source", "confidence", "revoked_at"].map(key => <td key={key}>{entry[key]}</td>)}</tr>)}</tbody></table></div>
          <div className="action-row"><button className="button button-small button-ghost" onClick={() => run(() => review("confirm"))}>确认勾选建议</button><button className="button button-small button-ghost" onClick={() => run(() => review("reject"))}>拒绝勾选建议</button></div>
        </div>
      </div>
    </section>

    <section className="surface taxonomy-panel">
      <div className="section-heading"><div><p className="eyebrow">TAXONOMY</p><h2>标签目录与别名</h2></div><button className="button button-small button-ghost" onClick={() => run(m.loadTags)}><Icon name="refresh" size={14} />刷新标签目录</button></div>
      <div className="taxonomy-grid">
        <div><Table headers={["ID", "名称", "命名空间", "状态", "使用量", "别名"]} rows={m.tags} columns={["tag_id", "name", "namespace", "status", "usage_count", "aliases"]} /></div>
        <div className="alias-editor"><div className="field-grid">{field("aliasName", "规范标签名")}{field("aliasValue", "别名")}</div><div className="action-row"><button className="button button-small button-ghost" onClick={() => run(m.loadAliases)}>查询别名</button><button className="button button-small button-ghost" onClick={() => run(() => alias("add"))}>添加别名</button><button className="button button-small button-ghost" onClick={() => run(() => alias("remove"))}>移除别名</button></div><Table headers={["别名", "规范标签名", "命名空间"]} rows={m.aliases} columns={["alias", "name", "namespace"]} /></div>
      </div>
    </section>
  </div>;
}

function Media({ part, media }) {
  const [failed, setFailed] = useState(false);
  const url = `/api/v1/media/${part.content_id}/${media.media_id}`;
  const preview = ["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"].includes(media.mime_detected);
  return <figure className="media-figure"><a className="media-link" href={url} target="_blank" rel="noopener"><Icon name="image" size={14} />媒体 {media.media_id} ({media.mime_detected})<Icon name="external" size={12} /></a>{preview && !failed ? <img src={url} alt={`归档图片 ${media.media_id}`} loading="lazy" onError={() => setFailed(true)} /> : <figcaption>读取失败或浏览器暂不支持此格式</figcaption>}</figure>;
}
