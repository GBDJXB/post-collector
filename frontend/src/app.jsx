import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router";
import { ModelPage, proposalProofs } from "./model.jsx";
import { CompositionsPage } from "./compositions.jsx";
import { CompositionDetailPage } from "./composition_detail.jsx";
import { LinkedText, safeWebUrl } from "./links.jsx";
import { HomePage } from "./home.jsx";
import { describeError, formatDate, formatToday, I18nProvider, localizedError, useI18n } from "./i18n/index.jsx";
import {
  compactText, EmptyState, Icon, LanguageSwitch, PageHeader, statusLabel,
  TagList, TypeMark,
} from "./ui.jsx";

const lines = value => value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
const emptyDraft = { text: "", note: "", images: [], entries: [], orderTouched: false, createComposition: false, compositionName: "", compositionDescription: "", compositionTags: "" };
const initialSearch = { q: "", kind: "all", source: "", tags: "", item_types: [], since: "", until: "", time_field: "ingest", sort: "time", limit: 40, include_duplicates: false };

const navigation = [
  { to: "/", labelKey: "nav.workbench.label", icon: "home", end: true, hintKey: "nav.workbench.hint" },
  { to: "/add", labelKey: "nav.add.label", icon: "plus", hintKey: "nav.add.hint" },
  { to: "/manage", labelKey: "nav.manage.label", icon: "search", hintKey: "nav.manage.hint" },
  { to: "/compositions", labelKey: "nav.compositions.label", icon: "layers", hintKey: "nav.compositions.hint" },
  { to: "/model", labelKey: "nav.model.label", icon: "sparkles", hintKey: "nav.model.hint" },
];

export function safeId(value) {
  const id = Number(value);
  if (!Number.isSafeInteger(id) || id <= 0) {
    const error = new Error("ID 必须为浏览器能够精确表示的正整数");
    error.code = "invalid_id";
    throw error;
  }
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
  if (pathname.startsWith("/compositions/")) return { eyebrowKey: "shell.page.composition.eyebrow", titleKey: "shell.page.composition.title" };
  return navigation.find(item => item.end ? pathname === item.to : pathname.startsWith(item.to)) || navigation[0];
}

export function Shell() {
  return <I18nProvider><ShellLayout /></I18nProvider>;
}

function ShellLayout() {
  const location = useLocation();
  const { t, lang } = useI18n();
  const [csrf, setCsrf] = useState("");
  const [session, setSession] = useState({ state: "checking" });
  const [notice, setNotice] = useState(null);
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
    if (response.status === 401) {
      setCsrf(""); csrfRef.current = ""; setSession({ state: "expired" });
    }
    if (!response.ok) {
      const error = new Error("request failed");
      error.code = "api"; error.status = response.status;
      error.invalidJson = invalidJson;
      error.detail = typeof payload.detail === "string" ? payload.detail : "";
      error.copied = Boolean(payload.copied);
      throw error;
    }
    if ((invalidJson && response.status !== 204) || (!invalidJson && (data === null || typeof data !== "object" || Array.isArray(data)))) {
      const error = new Error("bad payload");
      error.code = "bad_payload"; error.status = response.status;
      throw error;
    }
    return payload;
  }, []);
  const run = async operation => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setNotice({ tone: "progress", key: "shell.progress" });
    try { await operation(); setNotice({ tone: "info", key: "shell.done" }); }
    catch (error) { setNotice({ tone: "error", error }); }
    finally { lock.current = false; setBusy(false); }
  };
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
    if (token) window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);
    api("session", token ? { token } : undefined).then(data => {
      csrfRef.current = data.csrf; setCsrf(data.csrf);
      setSession({ state: "ready" });
    }).catch(error => { if (error.status !== 401) setSession({ state: "error", error }); });
  }, [api]);
  const manage = useManage(api);
  useEffect(() => {
    if (!notice || notice.tone === "progress") return;
    const timer = window.setTimeout(() => setNotice(null), 6500);
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
    const fail = error => setNotice({ tone: "error", error });
    try {
      if (messageId) manage.show({ message_id: safeId(messageId) }).catch(fail);
      else if (contentId) manage.show({ content_id: safeId(contentId) }).catch(fail);
      else if (compositionId) manage.showComposition(safeId(compositionId)).catch(fail);
    } catch (error) { fail(error); }
  }, [csrf, location.pathname, location.search]);
  useEffect(() => {
    const page = currentPage(location.pathname);
    document.title = `${t(page.labelKey || page.titleKey)} · 拾页 post-collector`;
  }, [location.pathname, t]);
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
    if (!draft.text.trim() && !images.length && !draft.entries.some(entry => entry.kind === "text" && entry.text.trim() || entry.kind === "link" && entry.url.trim())) throw localizedError("add.errorNoContent");
    if (images.length > 19) throw localizedError("add.errorTooManyImages");
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
  const today = formatToday(lang);
  const sessionText = session.state === "ready" ? t("shell.session.ready")
    : session.state === "expired" ? t("shell.session.expired")
    : session.state === "loggedOut" ? t("shell.session.loggedOut")
    : session.state === "error" ? describeError(session.error, t)
    : t("shell.session.checking");
  const noticeText = notice ? (notice.error ? describeError(notice.error, t) : notice.key ? t(notice.key) : "") : "";

  return <div className={`app-shell${csrf ? " is-ready" : " is-booting"}`}>
    <aside className="sidebar">
      <Link to="/" className="brand" aria-label={t("shell.brandAria")}>
        <span className="brand-mark">拾</span>
        <span className="brand-copy"><strong>拾页</strong><small>POST COLLECTOR</small></span>
      </Link>
      <Link to="/add" className="sidebar-add"><span><Icon name="plus" size={17} /></span><span><strong>{t("shell.addTitle")}</strong><small>{t("shell.addHint")}</small></span></Link>
      <nav className="sidebar-nav" aria-label={t("shell.navAria")}>
        <p className="nav-label">LIBRARY</p>
        {navigation.map((item, index) => <NavLink key={item.to} to={item.to} end={item.end} aria-label={t(item.labelKey)} className={({ isActive }) => `nav-item${isActive ? " is-active" : ""}`}>
          <span className="nav-icon"><Icon name={item.icon} size={17} /></span>
          <span className="nav-copy"><strong>{t(item.labelKey)}</strong><small>{t(item.hintKey)}</small></span>
          <span className="nav-index">{String(index + 1).padStart(2, "0")}</span>
        </NavLink>)}
      </nav>
      <div className="sidebar-foot">
        <div className="session-card">
          <span className={`status-dot${csrf ? " is-live" : ""}`} />
          <div><strong>{csrf ? t("shell.session.online") : t("shell.session.waiting")}</strong><p id="session-status">{sessionText}</p></div>
        </div>
        {csrf && <button className="sidebar-logout" disabled={busy} onClick={() => run(async () => { await api("logout", {}); setCsrf(""); csrfRef.current = ""; setSession({ state: "loggedOut" }); })}><Icon name="logout" size={15} />{t("shell.logout")}</button>}
        <p className="shortcut-note"><kbd>Ctrl</kbd><span>+</span><kbd>K</kbd> {t("shell.shortcut")}</p>
      </div>
    </aside>

    {csrf && <main className="app-main">
      <div className="topbar">
        <div className="topbar-route"><span>{today}</span><i /><span>{t(page.hintKey || page.eyebrowKey)}</span></div>
        <div className="topbar-tools">
          <span className="local-badge"><span className="status-dot is-live" />{t("shell.localOnly")}</span>
          <Link to="/manage" className="topbar-search"><Icon name="search" size={15} />{t("shell.searchLink")}<kbd>⌘K</kbd></Link>
          <LanguageSwitch />
        </div>
      </div>
      <p id="notice" role="status" aria-live="polite" className={`notice${noticeText ? " is-visible" : ""}${notice?.tone === "error" ? " is-error" : ""}`}>{noticeText}</p>
      <fieldset className="workspace" disabled={busy}>
        <Routes>
          <Route path="/" element={<HomePage api={api} />} />
          <Route path="/add" element={<AddPage draft={draft} setDraft={setDraft} addEntry={addEntry} updateEntry={updateEntry} moveEntry={moveEntry} fileKey={fileKey} submit={submit} run={run} upload={upload} />} />
          <Route path="/manage" element={<Manage state={manage} run={run} api={api} />} />
          <Route path="/compositions" element={<CompositionsPage api={api} run={run} parseId={safeId} />} />
          <Route path="/compositions/:composition_id" element={<CompositionDetailPage api={api} run={run} parseId={safeId} />} />
          <Route path="/model" element={<ModelPage api={api} run={run} parseId={safeId} />} />
          <Route path="*" element={<div className="page"><EmptyState icon="warning" title={t("shell.notFound.title")} action={<Link className="button button-primary" to="/">{t("shell.notFound.action")}</Link>}>{t("shell.notFound.body")}</EmptyState></div>} />
        </Routes>
      </fieldset>
    </main>}
  </div>;
}

function AddPage({ draft, setDraft, addEntry, updateEntry, moveEntry, fileKey, submit, run, upload }) {
  const { t } = useI18n();
  const displayImages = draft.orderTouched ? draft.entries.filter(entry => entry.kind === "image").map(entry => entry.file) : draft.images;
  const partsCount = (draft.text.trim() ? 1 : 0) + draft.entries.length;
  return <div className="page add-page">
    <PageHeader eyebrow={t("add.eyebrow")} title={t("add.title")} description={t("add.description")} actions={<Link className="button button-ghost" to="/manage"><Icon name="library" size={16} />{t("add.viewArchive")}</Link>} />
    <form className="composer-layout" onSubmit={e => { e.preventDefault(); run(submit); }}>
      <section className="surface composer-main">
        <div className="composer-toolbar">
          <div className="composer-label"><span className="composer-dot" /><span>{t("add.messageLabel")}</span></div>
          <span className="composer-count">{partsCount ? t("add.partsCount", { count: partsCount }) : t("add.waiting")}</span>
        </div>

        <label className="field field-body">
          <span className="field-label">{t("add.textLabel")}</span>
          <textarea name="text" rows="8" value={draft.text} onChange={e => setDraft({ ...draft, text: e.target.value })} placeholder={t("add.textPlaceholder")} />
        </label>

        <div className="media-drop-row">
          <label className="file-drop" htmlFor="composer-images">
            <input id="composer-images" key={fileKey} name="images" type="file" multiple accept=".png,.jpg,.jpeg,.gif,.webp,.avif,.heic,.heif" aria-label={t("add.imageAria")} onChange={e => { const files = Array.from(e.target.files); setDraft(current => ({ ...current, images: [...current.images, ...files], entries: [...current.entries, ...files.map(file => ({ kind: "image", file }))] })); }} />
            <span className="file-drop-icon"><Icon name="image" size={22} /></span>
            <span className="file-drop-copy"><strong>{t("add.addImages")}</strong><small>{t("add.imageFormats")}</small></span>
            <span className="file-drop-action">{t("add.chooseFiles")}</span>
          </label>
          <div className="add-part-actions">
            <button type="button" className="button button-ghost" onClick={() => addEntry({ kind: "text", text: "" })}><Icon name="text" size={15} />{t("add.addTextEntry")}</button>
            <button type="button" className="button button-ghost" onClick={() => addEntry({ kind: "link", url: "", description: "" })}><Icon name="link" size={15} />{t("add.addLinkEntry")}</button>
          </div>
        </div>

        {displayImages.length > 0 && <ul className="file-preview-list">{displayImages.map((file, index) => <li key={`${file.name}-${index}`}><span className="file-chip-icon"><Icon name="image" size={14} /></span><span>{t("add.filePreview", { name: file.name, size: file.size })}</span></li>)}</ul>}

        {draft.entries.length > 0 && <div className="ordered-editor">
          <div className="ordered-editor-heading"><div><p className="eyebrow">{t("add.orderedParts")}</p><h3>{t("add.entriesTitle")}</h3></div><span>{t("add.entriesNote")}</span></div>
          <ol className="entry-list">
            {draft.entries.map((entry, index) => <li key={index} className={`entry-item entry-${entry.kind}`}>
              <span className="entry-handle"><Icon name="drag" size={16} /></span>
              <span className="entry-number">{String(index + 1).padStart(2, "0")}</span>
              <div className="entry-body">
                {entry.kind === "text" && <label className="field"><span className="field-label">{t("add.entryTextLabel", { index: index + 1 })}</span><textarea value={entry.text} onChange={e => updateEntry(index, { text: e.target.value })} placeholder={t("add.entryTextPlaceholder")} /></label>}
                {entry.kind === "link" && <div className="entry-link-fields"><label className="field"><span className="field-label">{t("add.entryLinkLabel", { index: index + 1 })}</span><input value={entry.url} onChange={e => updateEntry(index, { url: e.target.value })} placeholder="https://…" /></label><label className="field"><span className="field-label">{t("add.entryLinkDescLabel", { index: index + 1 })}</span><textarea rows="2" value={entry.description} onChange={e => updateEntry(index, { description: e.target.value })} placeholder={t("add.entryLinkDescPlaceholder")} /></label></div>}
                {entry.kind === "image" && <div className="entry-image"><span className="file-chip-icon"><Icon name="image" size={15} /></span><span>{t("add.entryImage", { name: entry.file.name })}</span><small>{t("add.fileBytes", { size: entry.file.size })}</small></div>}
              </div>
              <div className="entry-actions">
                <button type="button" className="icon-button" aria-label={t("add.moveUp", { index: index + 1 })} disabled={index === 0} onClick={() => moveEntry(index, index - 1)}><Icon name="arrowUp" size={15} /></button>
                <button type="button" className="icon-button" aria-label={t("add.moveDown", { index: index + 1 })} disabled={index === draft.entries.length - 1} onClick={() => moveEntry(index, index + 1)}><Icon name="arrowDown" size={15} /></button>
                <button type="button" className="icon-button is-danger" aria-label={t("add.removeEntry", { index: index + 1 })} onClick={() => setDraft(current => ({ ...current, entries: current.entries.filter((_, position) => position !== index), orderTouched: true }))}><Icon name="close" size={15} /></button>
              </div>
            </li>)}
          </ol>
        </div>}

        <div className="composer-footnote"><Icon name="archive" size={15} /><span>{t("add.footnote")}</span></div>
      </section>

      <aside className="composer-aside">
        <section className="surface side-card">
          <div className="section-heading"><div><p className="eyebrow">{t("add.archiveNotes")}</p><h2>{t("add.archiveSettings")}</h2></div></div>
          <label className="field"><span className="field-label">{t("add.noteLabel")}</span><textarea aria-label={t("add.noteLabel")} name="note" rows="3" maxLength="4096" value={draft.note} onChange={e => setDraft({ ...draft, note: e.target.value })} placeholder={t("add.notePlaceholder")} /><span className="field-help">{t("add.noteHelp")}</span></label>
          <label className="switch-row"><span><strong>{t("add.createComposition")}</strong><small>{t("add.createCompositionHint")}</small></span><input type="checkbox" checked={draft.createComposition} onChange={e => setDraft({ ...draft, createComposition: e.target.checked })} /></label>
          {draft.createComposition && <div className="composition-create-fields">
            <label className="field"><span className="field-label">{t("add.compositionName")}</span><input value={draft.compositionName} maxLength="255" onChange={e => setDraft({ ...draft, compositionName: e.target.value })} placeholder={t("add.compositionNamePlaceholder")} /></label>
            <label className="field"><span className="field-label">{t("add.compositionDescription")}</span><textarea value={draft.compositionDescription} onChange={e => setDraft({ ...draft, compositionDescription: e.target.value })} placeholder={t("add.compositionDescriptionPlaceholder")} /></label>
            <label className="field"><span className="field-label">{t("add.compositionTags")}</span><textarea aria-label={t("add.compositionTags")} value={draft.compositionTags} onChange={e => setDraft({ ...draft, compositionTags: e.target.value })} placeholder={t("add.compositionTagsPlaceholder")} /><span className="field-help">{t("add.compositionTagsHelp")}</span></label>
          </div>}
        </section>
        <section className="surface side-card submission-card">
          <p className="eyebrow">{t("add.readyEyebrow")}</p>
          <h2>{partsCount ? t("add.readyTitle") : t("add.emptyTitle")}</h2>
          <dl className="mini-stats"><div><dt>{t("add.statParts")}</dt><dd>{partsCount}</dd></div><div><dt>{t("add.statImages")}</dt><dd>{displayImages.length}</dd></div><div><dt>{t("add.statMode")}</dt><dd>{draft.createComposition ? t("add.modeComposition") : t("add.modeMessage")}</dd></div></dl>
          <button type="submit" className="button button-primary button-wide"><Icon name="plus" size={16} />{t("add.submit")}</button>
          <p className="safe-note"><Icon name="shield" size={14} />{t("add.safeNote")}</p>
        </section>
        {upload && <section className="surface upload-result">
          <div className="upload-result-head"><span className={`status-dot${upload.status === "succeeded" ? " is-live" : ""}`} /><strong>{t("add.lastSubmit")}</strong></div>
          <p>{t("add.uploadMessage", { id: upload.message_id })}{upload.duplicate ? t("add.duplicate") : ""}{upload.detail} ({statusLabel(upload.status, t)})</p>
          {upload.content_ids?.length > 0 && <Link to={`/manage?message_id=${upload.message_id}`} className="text-link">{t("add.viewMessage")} <Icon name="arrowRight" size={14} /></Link>}
          {upload.composition_id && <Link to={`/compositions/${upload.composition_id}`} className="text-link">{t("add.openComposition")} <Icon name="arrowRight" size={14} /></Link>}
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
    if (!ids.length) throw localizedError("manage.selectTarget");
    return { [fields.tagTarget === "messages" ? "message_ids" : "content_ids"]: ids };
  }
  async function loadAssignments() { setAssignments((await api(`assignments?${query(targets())}`)).assignments); setSuggestions([]); }
  async function loadAliases() { setAliases((await api(`aliases?${query({ name: fields.aliasName })}`)).aliases); }
  async function refresh() { await loadTags(); if (target) await show(target); await find(); setAssignments([]); setSuggestions([]); }
  return { form, setForm, result, detail, compositionDetail, target, messages, setMessages, parts, setParts, suggestions, setSuggestions, tags, assignments, aliases, setAliases, fields, setFields, find, show, showComposition, loadTags, targets, loadAssignments, loadAliases, refresh };
}

function Manage({ state: m, run, api }) {
  const [activeIndex, setActiveIndex] = useState(-1);
  const { t, lang } = useI18n();
  const field = (key, label, type = "text", props = {}) => <label className="field"><span className="field-label">{label}</span><input type={type} value={m.fields[key]} onChange={e => m.setFields({ ...m.fields, [key]: e.target.value })} {...props} /></label>;
  const select = (key, label, choices) => <label className="field"><span className="field-label">{label}</span><select value={m.fields[key]} onChange={e => m.setFields({ ...m.fields, [key]: e.target.value })}>{choices.map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>;
  const searchField = (key, label, type = "text", props = {}) => <label className="field"><span className="field-label">{label}</span><input name={key} type={type} value={m.form[key]} onChange={e => m.setForm({ ...m.form, [key]: e.target.value })} {...props} /></label>;
  async function action(kind) {
    let target = m.target;
    if (m.fields.actionTarget === "parts") {
      if (m.parts.length !== 1) throw localizedError("manage.pickParts");
      target = { content_id: m.parts[0] };
    }
    if (!target) throw localizedError("manage.lookupFirst");
    await api(`actions/${kind}`, { ...target, copy: m.fields.copy });
  }
  async function tag(operation) { await api(`tags/${operation}`, { ...m.targets(), names: lines(m.fields.names), namespace: m.fields.namespace }); await m.refresh(); }
  async function alias(operation) {
    await api(`aliases/${operation}`, { alias: m.fields.aliasValue, name: m.fields.aliasName || null });
    await m.loadTags(); if (m.fields.aliasName.trim()) await m.loadAliases(); else m.setAliases([]);
  }
  async function review(operation) {
    if (!m.suggestions.length) throw localizedError("manage.selectSuggestions");
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
    <PageHeader eyebrow={t("manage.eyebrow")} title={t("manage.title")} description={t("manage.description")} actions={<Link className="button button-ghost" to="/compositions"><Icon name="layers" size={16} />{t("manage.organize")}</Link>} />
    <div className="manage-viewport" role="region" aria-label={t("manage.regionAria")} tabIndex={0}>
      <div className="manage-layout">
        <section className="surface search-panel">
          <div className="section-heading search-heading"><div><p className="eyebrow">{t("manage.searchEyebrow")}</p><h2>{t("manage.searchHeading")}</h2></div><span className="result-mode">{isV2 ? t("manage.modeV2") : t("manage.modeV1")}</span></div>
          <form id="search-form" className="search-form" onSubmit={e => { e.preventDefault(); setActiveIndex(-1); run(() => m.find()); }}>
            <div className="search-primary-row">
              <label className="search-input"><Icon name="search" size={18} /><span className="sr-only">{t("manage.keyword")}</span><input data-search-input aria-label={t("manage.keyword")} name="q" type="text" maxLength="4096" value={m.form.q} onChange={e => m.setForm({ ...m.form, q: e.target.value })} placeholder={t("manage.searchPlaceholder")} /><kbd>⌘K</kbd></label>
              <label className="compact-select"><span className="sr-only">{t("manage.resultKindAria")}</span><select aria-label={t("manage.resultKindAria")} value={m.form.kind} onChange={e => m.setForm({ ...m.form, kind: e.target.value })}><option value="all">{t("manage.kindAll")}</option><option value="content">{t("manage.kindContent")}</option><option value="composition">{t("manage.kindComposition")}</option></select></label>
              <button className="button button-primary" type="submit">{t("manage.searchSubmit")}</button>
            </div>
            <div className="filter-grid">
              {searchField("source", t("manage.sourceLabel"))}
              <label className="field filter-tags"><span className="field-label">{t("manage.tagsLabel")}</span><textarea name="tags" rows="1" value={m.form.tags} onChange={e => m.setForm({ ...m.form, tags: e.target.value })} placeholder={t("manage.tagsPlaceholder")} /></label>
              {searchField("since", t("manage.sinceLabel"), "date")}
              {searchField("until", t("manage.untilLabel"), "date")}
              <label className="field"><span className="field-label">{t("manage.timeFieldLabel")}</span><select name="time_field" value={m.form.time_field} onChange={e => m.setForm({ ...m.form, time_field: e.target.value })}><option value="ingest">{t("manage.timeIngest")}</option><option value="sent">{t("manage.timeSent")}</option></select></label>
              <label className="field"><span className="field-label">{t("manage.sortLabel")}</span><select name="sort" value={m.form.sort} onChange={e => m.setForm({ ...m.form, sort: e.target.value })}><option value="time">{t("manage.sortTime")}</option></select></label>
              {searchField("limit", t("manage.limitLabel"), "number", { min: 1, max: 50 })}
            </div>
            <fieldset className="type-filter"><legend>{t("manage.typesLegend")}</legend>{[["text", t("manage.typeText")], ["image", t("type.image")], ["mixed", t("manage.typeMixed")], ["video", t("type.video")], ["audio", t("type.audio")], ["link", t("type.link")], ["sticker", t("type.sticker")], ["file", t("type.file")]].map(([value, label]) => <label key={value} className={`filter-chip${m.form.item_types.includes(value) ? " is-selected" : ""}`}><input type="checkbox" name="item_types" checked={m.form.item_types.includes(value)} onChange={e => m.setForm({ ...m.form, item_types: e.target.checked ? [...m.form.item_types, value] : m.form.item_types.filter(x => x !== value) })} />{label}</label>)}</fieldset>
          </form>
          {m.result && <div className="result-toolbar"><p id="search-summary">{t("manage.summary", { page: m.result.page, count: hits.length, total: m.result.total ?? t("manage.unassignedTotal"), kind: m.result.total_kind || "estimate" })}{m.result.incomplete ? t("manage.summaryTruncated") : ""}</p><span>{isV2 ? t("manage.listV2") : t("manage.listV1")}</span></div>}
          <div className="table-wrap result-table-wrap">
            <table className="result-table">
              <thead><tr>{[t("manage.colSelect"), isV2 ? t("manage.colResultId") : t("manage.colMessageId"), t("manage.colType"), t("manage.colContent"), t("manage.colTags"), t("manage.colSource"), t("manage.colTime"), t("manage.colDetail")].map(x => <th key={x}>{x}</th>)}</tr></thead>
              <tbody id="hits">
                {isV2 ? hits.map((hit, index) => <tr key={`${hit.kind}:${hit.entity_id}`} className={`hit-row hit-${hit.kind}${activeIndex === index ? " is-active" : ""}`} data-type={hit.kind === "composition" ? "composition" : hit.item_type}>
                  <td className="hit-select">{hit.kind === "content" && <Selection ids={m.parts} setIds={m.setParts} id={hit.content_id} label={t("manage.selectContent", { id: hit.content_id })} />}</td>
                  <td className="hit-ref">{hit.kind === "composition" ? <><span className="hit-kind">{t("manage.hitComposition")}</span><strong>#{hit.composition_id}</strong></> : <><span className="hit-kind">{t("manage.hitContent")}</span><strong>#{hit.content_id}</strong></>}</td>
                  <td className="hit-type"><TypeMark kind={hit.kind} type={hit.item_type} compact /></td>
                  <td className="summary"><span className="summary-clamp">{hit.kind === "composition" ? hit.name : (hit.text || hit.original_name || "").slice(0, 180)}</span>{hit.kind === "composition" && hit.description && <small className="summary-sub">{compactText(hit.description, 90)}</small>}</td>
                  <td className="hit-tags"><TagList tags={hit.tags || []} limit={4} empty={<span className="muted">{t("manage.noTags")}</span>} /></td>
                  <td className="hit-source">{hit.source_name || (hit.kind === "composition" ? t("manage.hitComposition") : "—")}</td>
                  <td className="hit-time"><span>{formatDate(hit.sort_time, true, lang)}</span></td>
                  <td className="hit-actions"><button className="button button-small button-ghost" aria-label={hit.kind === "composition" ? t("manage.viewComposition", { id: hit.composition_id }) : t("manage.viewContent", { id: hit.content_id })} onClick={() => run(() => openHit(hit, index))}><Icon name="eye" size={14} /><span>{t("manage.viewAction")}</span></button>{hit.kind === "composition" && <Link className="quiet-link hit-manage-link" to={`/compositions/${hit.composition_id}`}>{t("manage.manageComposition", { id: hit.composition_id })}</Link>}</td>
                </tr>) : hits.map((hit, index) => <tr key={hit.message_id} className={`hit-row hit-message${activeIndex === index ? " is-active" : ""}`} data-type={hit.item_type}>
                  <td className="hit-select"><Selection ids={m.messages} setIds={m.setMessages} id={hit.message_id} label={t("manage.selectMessage", { id: hit.message_id })} /></td>
                  <td className="hit-ref"><span className="hit-kind">{t("manage.hitMessage")}</span><strong>#{hit.message_id}</strong></td>
                  <td className="hit-type"><TypeMark type={hit.item_type} compact /></td>
                  <td className="summary"><span className="summary-clamp">{(hit.snippet || hit.text || hit.original_name || "").slice(0, 180)}</span></td>
                  <td className="hit-tags"><TagList tags={(hit.tags || []).map(tag => typeof tag === "string" ? { name: tag } : tag)} limit={4} empty={<span className="muted">{t("manage.noTags")}</span>} /></td>
                  <td className="hit-source">{hit.source_name || hit.source || hit.source_id || "—"}</td>
                  <td className="hit-time"><span>{formatDate(hit.first_seen_at, true, lang)}</span></td>
                  <td className="hit-actions"><button className="button button-small button-ghost" onClick={() => run(() => { setActiveIndex(index); return m.show({ message_id: hit.message_id }); })}><Icon name="eye" size={14} /><span>{t("manage.viewAction")}</span></button></td>
                </tr>)}
              </tbody>
            </table>
            {m.result && !hits.length && <EmptyState icon="search" title={t("manage.emptyTitle")}><p>{t("manage.emptyBody")}</p></EmptyState>}
          </div>
          <div className="result-footer"><button id="next" className="button button-ghost" disabled={!m.result?.has_more} onClick={() => run(() => m.find(true))}>{t("manage.nextPage")} <Icon name="arrowRight" size={15} /></button>{m.result?.has_more && <span>{t("manage.more")}</span>}</div>
        </section>

        <section className="surface manage-detail detail-pane">
          <div className="detail-heading"><div><p className="eyebrow">{t("manage.detailEyebrow")}</p><h2>{t("manage.detailHeading")}</h2></div>{activeIndex >= 0 && <div className="detail-steps"><button className="icon-button" aria-label={t("manage.prev")} disabled={activeIndex <= 0} onClick={() => stepHit(-1)}><Icon name="chevronLeft" size={17} /></button><span>{activeIndex + 1} / {hits.length}</span><button className="icon-button" aria-label={t("manage.next")} disabled={activeIndex >= hits.length - 1} onClick={() => stepHit(1)}><Icon name="chevronRight" size={17} /></button></div>}</div>
          <details className="id-lookup"><summary><Icon name="hash" size={14} />{t("manage.lookupSummary")}</summary><form onSubmit={e => { e.preventDefault(); setActiveIndex(-1); run(() => m.show({ [m.fields.detailKind]: safeId(m.fields.detailId) })); }}>
            {select("detailKind", t("manage.detailTarget"), [["message_id", t("manage.detailMessageId")], ["content_id", t("manage.detailContentId")]])}{field("detailId", t("manage.idLabel"), "number", { min: 1, required: true })}<button className="button button-small button-ghost" type="submit">{t("manage.viewAction")}</button></form></details>
          {!m.detail && !m.compositionDetail && <EmptyState icon="bookOpen" title={t("manage.readerEmptyTitle")}><p>{t("manage.readerEmptyBody")}</p></EmptyState>}
          {m.detail && <article className="reader">
            <header className="reader-header"><div className="reader-kicker"><span className="reader-rule" /><span>{t("manage.messageTitle", { id: m.detail.message_id })}</span><span>·</span><span>{m.detail.source}</span></div><h3>{compactText((m.detail.parts || []).find(part => part.text)?.text, 70) || t("manage.untitled")}</h3><div className="reader-meta"><span><Icon name="clock" size={13} />{formatDate(m.detail.sent_at || m.detail.parts?.[0]?.first_seen_at, true, lang)}</span>{m.detail.origin_note && <span className="reader-note"><Icon name="pin" size={13} />{m.detail.origin_note}</span>}</div></header>
            <div id="parts" className="reader-parts">{(m.detail.parts || []).map(part => <article className="reader-part" key={part.content_id} data-type={part.item_type}>
              <div className="reader-part-head"><label className="part-select"><Selection ids={m.parts} setIds={m.setParts} id={part.content_id} label={t("manage.selectContent", { id: part.content_id })} /><span>{t("manage.partLabel", { id: part.content_id, type: part.item_type })}</span></label>{part.original_name && <span className="reader-file">{part.original_name}</span>}</div>
              {part.url && safeWebUrl(part.url) && <a className="reader-link" href={safeWebUrl(part.url)} target="_blank" rel="noopener noreferrer"><Icon name="link" size={15} />{part.url}<Icon name="external" size={13} /></a>}
              {part.text && <pre><LinkedText text={part.text} /></pre>}
              {(part.media || []).map(media => <Media key={media.media_id} part={part} media={media} />)}
            </article>)}</div>
            <div className="reader-actions">{select("actionTarget", t("manage.systemTarget"), [["current", t("manage.actionCurrent")], ["parts", t("manage.actionParts")]])}<label className="checkbox-row"><input type="checkbox" checked={m.fields.copy} onChange={e => m.setFields({ ...m.fields, copy: e.target.checked })} />{t("manage.copyWith")}</label><div className="action-row">{[["open", t("manage.openAction"), "external"], ["reveal", t("manage.revealAction"), "archive"], ["copy", t("manage.copyAction"), "copy"]].map(([value, label, icon]) => <button className="button button-ghost" key={value} onClick={() => run(() => action(value))}><Icon name={icon} size={15} />{label}</button>)}</div><p className="field-help">{t("manage.actionsHelp")}</p></div>
          </article>}
          {m.compositionDetail && <article className="reader composition-reader">
            <header className="reader-header"><div className="reader-kicker"><span className="reader-rule" /><span>{t("manage.compositionKicker", { id: m.compositionDetail.composition_id })}</span><span>·</span><span>{t("manage.positionCount", { count: m.compositionDetail.members.length })}</span></div><h3>{m.compositionDetail.name}</h3>{m.compositionDetail.description && <p className="reader-description">{m.compositionDetail.description}</p>}<div className="reader-meta"><span><Icon name="layers" size={13} />{t("manage.readByPosition")}</span><Link className="quiet-link" to={`/compositions/${m.compositionDetail.composition_id}`}>{t("manage.manageComposition", { id: m.compositionDetail.composition_id })}</Link></div></header>
            <div className="own-tags-block"><span className="own-tags-label">{t("manage.ownTags")}</span><TagList tags={m.compositionDetail.tags || []} empty={<span className="muted">{t("manage.noOwnTags")}</span>} /><small>{t("manage.ownTagsNote")}</small></div>
            <div id="parts" className="reader-parts sequence-parts">{m.compositionDetail.members.map((member, index) => <article className="reader-part sequence-part" key={member.position} data-position={String(index + 1).padStart(2, "0")}>
              {member.available ? <>{t("manage.partLabel", { id: member.content_id, type: member.item_type })}{" "}{member.text && <pre><LinkedText text={member.text} /></pre>}{member.url && safeWebUrl(member.url) && <a className="reader-link" href={safeWebUrl(member.url)} target="_blank" rel="noopener noreferrer">{member.url}</a>}{(member.media || []).map(media => <Media key={media.media_id} part={member} media={media} />)}</> : t("manage.unavailablePosition", { position: member.position })}
            </article>)}</div>
          </article>}
        </section>
      </div>
    </div>

    <section className="surface tag-workbench">
      <div className="section-heading"><div><p className="eyebrow">{t("manage.annotateEyebrow")}</p><h2>{t("manage.tagHeading")}</h2></div><p>{t("manage.tagIntro")}</p></div>
      <div className="tag-workbench-grid">
        <div className="tag-controls">
          {select("tagTarget", t("manage.tagTarget"), isV2 ? [["parts", t("manage.tagTargetPartsV2")]] : [["messages", t("manage.tagTargetMessages")], ["parts", t("manage.tagTargetParts")]])}
          <label className="field"><span className="field-label">{t("manage.tagNames")}</span><textarea rows="3" value={m.fields.names} onChange={e => m.setFields({ ...m.fields, names: e.target.value })} placeholder={t("manage.tagNamesPlaceholder")} /></label>
          {field("namespace", t("manage.namespace"))}
          <div className="action-row"><button className="button button-primary" onClick={() => run(() => tag("add"))}><Icon name="tag" size={15} />{t("manage.addTag")}</button><button className="button button-ghost" onClick={() => run(() => tag("remove"))}>{t("manage.removeTag")}</button><button className="button button-ghost" onClick={() => run(m.loadAssignments)}>{t("manage.loadAssignments")}</button></div>
          <div className="selection-summary"><span>{t("manage.selection")}</span><strong>{m.fields.tagTarget === "messages" ? m.messages.length : m.parts.length}</strong><span>{t("manage.selectionUnit", { count: m.fields.tagTarget === "messages" ? m.messages.length : m.parts.length })}</span></div>
        </div>
        <div className="association-panel">
          <div className="section-heading mini"><h3>{t("manage.associations")}</h3><span>{t("manage.associationHint")}</span></div>
          <div className="table-wrap compact-table"><table><thead><tr>{[t("manage.colSuggestion"), t("manage.colAssignId"), t("manage.colContentId"), t("manage.colName"), t("manage.colSource"), t("manage.colConfidence"), t("manage.colRevoked")].map(x => <th key={x}>{x}</th>)}</tr></thead><tbody>{m.assignments.map(entry => <tr key={entry.assign_id}><td>{entry.source === "suggested" && !entry.revoked_at && <Selection ids={m.suggestions} setIds={m.setSuggestions} id={entry.assign_id} label={t("manage.selectSuggestion", { id: entry.assign_id })} />}</td>{["assign_id", "content_id", "name", "source", "confidence", "revoked_at"].map(key => <td key={key}>{entry[key]}</td>)}</tr>)}</tbody></table></div>
          <div className="action-row"><button className="button button-small button-ghost" onClick={() => run(() => review("confirm"))}>{t("manage.confirmSuggestions")}</button><button className="button button-small button-ghost" onClick={() => run(() => review("reject"))}>{t("manage.rejectSuggestions")}</button></div>
        </div>
      </div>
    </section>

    <section className="surface taxonomy-panel">
      <div className="section-heading"><div><p className="eyebrow">{t("manage.taxonomyEyebrow")}</p><h2>{t("manage.taxonomyHeading")}</h2></div><button className="button button-small button-ghost" onClick={() => run(m.loadTags)}><Icon name="refresh" size={14} />{t("manage.refreshTags")}</button></div>
      <div className="taxonomy-grid">
        <div><Table headers={[t("manage.colTagId"), t("manage.colTagName"), t("manage.colNamespace"), t("manage.colStatus"), t("manage.colUsage"), t("manage.colAliases")]} rows={m.tags} columns={["tag_id", "name", "namespace", "status", "usage_count", "aliases"]} /></div>
        <div className="alias-editor"><div className="field-grid">{field("aliasName", t("manage.aliasCanonical"))}{field("aliasValue", t("manage.colAlias"))}</div><div className="action-row"><button className="button button-small button-ghost" onClick={() => run(m.loadAliases)}>{t("manage.queryAlias")}</button><button className="button button-small button-ghost" onClick={() => run(() => alias("add"))}>{t("manage.addAlias")}</button><button className="button button-small button-ghost" onClick={() => run(() => alias("remove"))}>{t("manage.removeAlias")}</button></div><Table headers={[t("manage.colAlias"), t("manage.aliasCanonical"), t("manage.colNamespace")]} rows={m.aliases} columns={["alias", "name", "namespace"]} /></div>
      </div>
    </section>
  </div>;
}

function Media({ part, media }) {
  const { t } = useI18n();
  const [failed, setFailed] = useState(false);
  const url = `/api/v1/media/${part.content_id}/${media.media_id}`;
  const preview = ["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"].includes(media.mime_detected);
  return <figure className="media-figure"><a className="media-link" href={url} target="_blank" rel="noopener"><Icon name="image" size={14} />{t("manage.mediaLink", { id: media.media_id, mime: media.mime_detected })}<Icon name="external" size={12} /></a>{preview && !failed ? <img src={url} alt={t("manage.mediaAlt", { id: media.media_id })} loading="lazy" onError={() => setFailed(true)} /> : <figcaption>{t("manage.mediaUnsupported")}</figcaption>}</figure>;
}
