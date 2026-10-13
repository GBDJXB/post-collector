import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, Route, Routes, useLocation } from "react-router";
import { ModelPage, proposalProofs } from "./model.jsx";
import { CompositionsPage } from "./compositions.jsx";

const lines = value => value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
const emptyDraft = { text: "", note: "", images: [] };
const initialSearch = { q: "", source: "", tags: "", item_types: [], since: "", until: "", time_field: "ingest", sort: "time", limit: 40, include_duplicates: false };
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
function Table({ headers, rows, columns }) {
  return <div className="table-wrap"><table><thead><tr>{headers.map(h => <th key={h}>{h}</th>)}</tr></thead>
    <tbody>{rows.map((row, index) => <tr key={index}>{columns.map(key => <td key={key}>{Array.isArray(row[key]) ? row[key].join(" ") : row[key] ?? ""}</td>)}</tr>)}</tbody></table></div>;
}
function Selection({ ids, setIds, id, label }) {
  return <input type="checkbox" aria-label={label} checked={ids.includes(id)} onChange={e => setIds(e.target.checked ? [...ids, safeId(id)] : ids.filter(x => x !== id))} />;
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
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) { setCsrf(""); csrfRef.current = ""; setSession("会话失效，请使用服务终端中的新授权链接。"); }
      throw new Error((typeof data.detail === "string" ? data.detail : "参数无效，请检查输入") + (data.copied ? "；剪贴板复制已完成" : ""));
    }
    return data;
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
    }).catch(error => { setSession(error.message); });
  }, [api]);
  const manage = useManage(api);
  useEffect(() => {
    if (csrf) run(async () => { await manage.find(false); await manage.loadTags(); });
  }, [csrf]);
  useEffect(() => {
    const id = new URLSearchParams(location.search).get("message_id");
    if (csrf && location.pathname === "/manage" && id) {
      try { manage.show({ message_id: safeId(id) }).catch(error => setNotice(error.message)); }
      catch (error) { setNotice(error.message); }
    }
  }, [csrf, location.pathname, location.search]);
  async function submit() {
    if (!draft.text.trim() && !draft.images.length) throw new Error("请输入文本或选择图片");
    if (draft.images.length > 19) throw new Error("一次最多上传 19 张图片");
    const body = new FormData(); body.append("text", draft.text); body.append("note", draft.note);
    draft.images.forEach(file => body.append("images", file));
    const result = await api("uploads", body);
    setUpload(result);
    setDraft(emptyDraft); setFileKey(x => x + 1);
    await manage.find(false);
  }
  return <><h1>post-collector</h1><p id="session-status">{session}</p><p id="notice" role="status" aria-live="polite">{notice}</p>
    {csrf && <main id="workspace"><nav><Link to="/">入口</Link> | <Link to="/add">加入新内容</Link> | <Link to="/manage">检索 / 管理已有内容</Link> | <Link to="/compositions">整理组合</Link> | <Link to="/model">模型标签建议</Link></nav>
      <button disabled={busy} onClick={() => run(async () => { await api("logout", {}); setCsrf(""); csrfRef.current = ""; setSession("已退出，请重启服务获取新授权链接。"); })}>退出本机会话</button>
      <fieldset className="workspace" disabled={busy}><Routes>
        <Route path="/" element={<section><h2>内容归档</h2><p><Link to="/add">加入新内容</Link></p><p><Link to="/manage">检索 / 管理已有内容</Link></p></section>} />
        <Route path="/add" element={<section><h2>加入新内容</h2><form onSubmit={e => { e.preventDefault(); run(submit); }}>
          <label>文本 <textarea name="text" rows="8" value={draft.text} onChange={e => setDraft({ ...draft, text: e.target.value })} /></label>
          <label>图片 <input key={fileKey} name="images" type="file" multiple accept=".png,.jpg,.jpeg,.gif,.webp,.avif,.heic,.heif" onChange={e => setDraft({ ...draft, images: Array.from(e.target.files) })} /></label>
          <ul>{draft.images.map((file, i) => <li key={i}>{file.name} ({file.size} 字节)</li>)}</ul>
          <label>备注 <textarea name="note" rows="2" maxLength="4096" value={draft.note} onChange={e => setDraft({ ...draft, note: e.target.value })} /></label>
          <p>一次提交组成一条消息，最多 19 张图片。提交后自动扫描、去重并入库。</p><button type="submit">加入内容</button></form>
          {upload && <p role="status">消息 {upload.message_id}：{upload.duplicate ? "已存在；" : ""}{upload.detail} ({upload.status}) {upload.content_ids.length > 0 && <Link to={`/manage?message_id=${upload.message_id}`}>查看本次消息</Link>}</p>}
        </section>} />
        <Route path="/manage" element={<Manage state={manage} run={run} api={api} />} />
        <Route path="/compositions" element={<CompositionsPage api={api} run={run} parseId={safeId} />} />
        <Route path="/model" element={<ModelPage api={api} run={run} parseId={safeId} />} />
        <Route path="*" element={<p>页面不存在。<Link to="/">返回入口</Link></p>} />
      </Routes></fieldset></main>}
  </>;
}

function useManage(api) {
  const [form, setForm] = useState(initialSearch), [result, setResult] = useState(null);
  const [params, setParams] = useState(null), [detail, setDetail] = useState(null), [target, setTarget] = useState(null);
  const [messages, setMessages] = useState([]), [parts, setParts] = useState([]), [suggestions, setSuggestions] = useState([]);
  const [tags, setTags] = useState([]), [assignments, setAssignments] = useState([]), [aliases, setAliases] = useState([]);
  const [fields, setFields] = useState({ detailKind: "message_id", detailId: "", actionTarget: "current", copy: false, tagTarget: "messages", names: "", namespace: "general", aliasName: "", aliasValue: "" });
  async function find(next = false) {
    const values = next ? { ...params, page: result.page + 1, cursor: result.next_cursor || null } : { ...form, tags: lines(form.tags), page: 1 };
    const data = await api(`search?${query(values)}`); setResult(data); setParams(values); setMessages([]);
  }
  async function show(value) { const data = await api(`detail?${query(value)}`); setDetail(data); setTarget(value); setParts([]); }
  async function loadTags() { setTags((await api("tags")).tags); }
  function targets() {
    const ids = fields.tagTarget === "messages" ? messages : parts;
    if (!ids.length) throw new Error("请勾选标签操作目标");
    return { [fields.tagTarget === "messages" ? "message_ids" : "content_ids"]: ids };
  }
  async function loadAssignments() { setAssignments((await api(`assignments?${query(targets())}`)).assignments); setSuggestions([]); }
  async function loadAliases() { setAliases((await api(`aliases?${query({ name: fields.aliasName })}`)).aliases); }
  async function refresh() { await loadTags(); if (target) await show(target); await find(); setAssignments([]); setSuggestions([]); }
  return { form, setForm, result, detail, target, messages, setMessages, parts, setParts, suggestions, setSuggestions, tags, assignments, aliases, setAliases, fields, setFields, find, show, loadTags, targets, loadAssignments, loadAliases, refresh };
}

function Manage({ state: m, run, api }) {
  const field = (key, label, type = "text", props = {}) => <label>{label} <input type={type} value={m.fields[key]} onChange={e => m.setFields({ ...m.fields, [key]: e.target.value })} {...props} /></label>;
  const select = (key, label, choices) => <label>{label} <select value={m.fields[key]} onChange={e => m.setFields({ ...m.fields, [key]: e.target.value })}>{choices.map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>;
  const searchField = (key, label, type = "text", props = {}) => <label>{label} <input name={key} type={type} value={m.form[key]} onChange={e => m.setForm({ ...m.form, [key]: e.target.value })} {...props} /></label>;
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
  return <><section><h2>检索与最近消息</h2><form id="search-form" onSubmit={e => { e.preventDefault(); run(() => m.find()); }}>
    {searchField("q", "关键词", "text", { maxLength: 4096 })}{searchField("source", "来源名或 ID")}
    <label>标签（每行一个，全部匹配）<textarea name="tags" rows="2" value={m.form.tags} onChange={e => m.setForm({ ...m.form, tags: e.target.value })} /></label>
    <fieldset><legend>内容类型（不勾选表示全部）</legend>{[["text", "文本"], ["image", "图片"], ["mixed", "混合"], ["video", "视频"], ["audio", "音频"], ["link", "链接"], ["sticker", "贴纸"], ["file", "文件"]].map(([value, label]) => <label key={value}><input type="checkbox" name="item_types" checked={m.form.item_types.includes(value)} onChange={e => m.setForm({ ...m.form, item_types: e.target.checked ? [...m.form.item_types, value] : m.form.item_types.filter(x => x !== value) })} />{label}</label>)}</fieldset>
    {searchField("since", "开始日期", "date")}{searchField("until", "结束日期", "date")}
    <label>时间字段 <select name="time_field" value={m.form.time_field} onChange={e => m.setForm({ ...m.form, time_field: e.target.value })}><option value="ingest">入库时间</option><option value="sent">发送时间</option></select></label>
    <label>排序 <select name="sort" value={m.form.sort} onChange={e => m.setForm({ ...m.form, sort: e.target.value })}><option value="time">时间</option><option value="relevance">相关度</option></select></label>
    {searchField("limit", "每页", "number", { min: 1, max: 50 })}<label><input type="checkbox" checked={m.form.include_duplicates} onChange={e => m.setForm({ ...m.form, include_duplicates: e.target.checked })} />包括重复消息</label><button type="submit">检索 / 返回第一页</button></form>
    {m.result && <p id="search-summary">第 {m.result.page} 页，本页 {m.result.hits.length} 条；总数 {m.result.total ?? "未统计"} ({m.result.total_kind}){m.result.incomplete ? "；候选已截断" : ""}</p>}
    <div className="table-wrap"><table><thead><tr>{["选择", "消息 ID", "类型", "内容", "标签", "来源", "时间", "详情"].map(x => <th key={x}>{x}</th>)}</tr></thead><tbody id="hits">{(m.result?.hits || []).map(hit => <tr key={hit.message_id}>
      <td><Selection ids={m.messages} setIds={m.setMessages} id={hit.message_id} label={`选择消息 ${hit.message_id}`} /></td><td>{hit.message_id}</td><td>{hit.item_type}</td><td className="summary"><span className="summary-clamp">{(hit.snippet || hit.text || hit.original_name || "").slice(0, 180)}</span></td><td>{[...new Set(hit.tags.map(t => typeof t === "string" ? t : t.name))].join(" ")}</td><td>{hit.source_name || hit.source || hit.source_id}</td><td>{hit.first_seen_at}</td><td><button onClick={() => run(() => m.show({ message_id: hit.message_id }))}>查看</button></td></tr>)}</tbody></table></div>
    <button id="next" disabled={!m.result?.has_more} onClick={() => run(() => m.find(true))}>下一页</button></section>
    <section><h2>详情与本机操作</h2><form onSubmit={e => { e.preventDefault(); run(() => m.show({ [m.fields.detailKind]: safeId(m.fields.detailId) })); }}>
      {select("detailKind", "目标", [["message_id", "消息 ID"], ["content_id", "内容项 ID"]])}{field("detailId", "ID", "number", { min: 1, required: true })}<button type="submit">查看</button></form>
      {m.detail && <><p id="detail-summary">消息 {m.detail.message_id}；来源 {m.detail.source}；{m.detail.origin_note || ""}</p><div id="parts">{m.detail.parts.map(part => <article key={part.content_id}><label><Selection ids={m.parts} setIds={m.setParts} id={part.content_id} label={`选择内容项 ${part.content_id}`} />内容项 {part.content_id} ({part.item_type}) {part.original_name || ""}</label>
        {part.text && <pre>{part.text}</pre>}{part.media.map(media => <Media key={media.media_id} part={part} media={media} />)}</article>)}</div></>}
      {select("actionTarget", "系统操作目标", [["current", "当前查看目标"], ["parts", "详情中勾选的一个内容项"]])}<label><input type="checkbox" checked={m.fields.copy} onChange={e => m.setFields({ ...m.fields, copy: e.target.checked })} />打开 / 定位时同时复制</label>
      {[["open", "默认程序打开"], ["reveal", "文件管理器定位"], ["copy", "复制"]].map(([value, label]) => <button key={value} onClick={() => run(() => action(value))}>{label}</button>)}<p>文本复制正文；图片复制文件。混合内容打开为 HTML，暂不支持复制。</p></section>
    <section><h2>人工标签与关联</h2>{select("tagTarget", "标签操作目标", [["messages", "结果中勾选的消息"], ["parts", "详情中勾选的内容项"]])}
      <label>标签名称（每行一个）<textarea rows="3" value={m.fields.names} onChange={e => m.setFields({ ...m.fields, names: e.target.value })} /></label>{field("namespace", "命名空间")}
      <button onClick={() => run(() => tag("add"))}>添加标签</button><button onClick={() => run(() => tag("remove"))}>撤销标签</button><button onClick={() => run(m.loadAssignments)}>查看目标关联记录</button>
      <div className="table-wrap"><table><thead><tr>{["建议选择", "关联 ID", "内容 ID", "名称", "来源", "置信度", "撤销时间"].map(x => <th key={x}>{x}</th>)}</tr></thead><tbody>{m.assignments.map(entry => <tr key={entry.assign_id}><td>{entry.source === "suggested" && !entry.revoked_at && <Selection ids={m.suggestions} setIds={m.setSuggestions} id={entry.assign_id} label={`选择建议 ${entry.assign_id}`} />}</td>{["assign_id", "content_id", "name", "source", "confidence", "revoked_at"].map(key => <td key={key}>{entry[key]}</td>)}</tr>)}</tbody></table></div>
      <button onClick={() => run(() => review("confirm"))}>确认勾选建议</button><button onClick={() => run(() => review("reject"))}>拒绝勾选建议</button></section>
    <section><h2>标签目录与别名</h2><button onClick={() => run(m.loadTags)}>刷新标签目录</button><Table headers={["ID", "名称", "命名空间", "状态", "使用量", "别名"]} rows={m.tags} columns={["tag_id", "name", "namespace", "status", "usage_count", "aliases"]} />
      {field("aliasName", "规范标签名")}{field("aliasValue", "别名")}<button onClick={() => run(m.loadAliases)}>查询别名</button><button onClick={() => run(() => alias("add"))}>添加别名</button><button onClick={() => run(() => alias("remove"))}>移除别名</button><Table headers={["别名", "规范标签名", "命名空间"]} rows={m.aliases} columns={["alias", "name", "namespace"]} /></section>
  </>;
}

function Media({ part, media }) {
  const [failed, setFailed] = useState(false);
  const url = `/api/v1/media/${part.content_id}/${media.media_id}`;
  const preview = ["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"].includes(media.mime_detected);
  return <><a href={url} target="_blank" rel="noopener">媒体 {media.media_id} ({media.mime_detected})</a>{preview && !failed ? <img src={url} alt={`归档图片 ${media.media_id}`} loading="lazy" onError={() => setFailed(true)} /> : <span>；读取失败或浏览器暂不支持此格式</span>}</>;
}
