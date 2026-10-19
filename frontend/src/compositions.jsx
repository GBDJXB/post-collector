import React, { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { compactText, EmptyState, formatDate, Icon, InlineNotice, PageHeader, TagList, TypeMark } from "./ui.jsx";

const names = text => text.split(/\r?\n/).map(value => value.trim()).filter(Boolean);

function CandidateImages({ part }) {
  return (part.media?.items || []).filter(media => media.media_id && media.mime_detected?.startsWith("image/")).map(media => <img key={media.media_id} className="composition-image" src={`/api/v1/media/${part.content_id}/${media.media_id}`} alt={`内容项 ${part.content_id} 图片`} loading="lazy" />);
}

function QueueImages({ item }) {
  return (item.media?.items || []).filter(media => media.media_id && media.mime_detected?.startsWith("image/")).map(media => <img key={media.media_id} className="composition-image" src={`/api/v1/media/${item.content_id}/${media.media_id}`} alt={`内容项 ${item.content_id} 图片`} loading="lazy" />);
}

export function CompositionsPage({ api, run, parseId }) {
  const [term, setTerm] = useState("");
  const [result, setResult] = useState(null);
  const [queue, setQueue] = useState([]);
  const [name, setName] = useState(""), [description, setDescription] = useState(""), [tags, setTags] = useState("");
  const [created, setCreated] = useState(null), [savedDetail, setSavedDetail] = useState(null), [warning, setWarning] = useState("");
  const [filter, setFilter] = useState(""), [memberTags, setMemberTags] = useState(false), [existing, setExisting] = useState(null);
  const [lookupName, setLookupName] = useState(""), [lookupId, setLookupId] = useState("");
  const [directoryError, setDirectoryError] = useState("");
  const serial = useRef(0), dragged = useRef(null), active = useRef(true), submittedTerm = useRef("");
  const directoryRequest = useRef(0);
  const submittedDirectory = useRef({ name: "", id: "", tags: [], memberTags: false });

  useEffect(() => {
    active.current = true;
    api("search?sort=time&limit=40&page=1").then(data => { if (active.current) setResult(data); }).catch(error => { if (active.current) setWarning(error.message); });
    const request = ++directoryRequest.current;
    api("compositions?limit=50&offset=0").then(data => { if (active.current && directoryRequest.current === request) setExisting(data); }).catch(error => { if (active.current && directoryRequest.current === request) setDirectoryError(error.message); });
    return () => { active.current = false; ++directoryRequest.current; };
  }, [api]);

  async function search(next = false) {
    const params = new URLSearchParams({ q: next ? submittedTerm.current : term, sort: "time", limit: "40", page: String(next ? result.page + 1 : 1) });
    if (next && result.next_cursor) params.set("cursor", result.next_cursor);
    const response = await api(`search?${params}`);
    if (!next) submittedTerm.current = term;
    setResult(response);
  }
  async function add(part) {
    const id = parseId(part.content_id);
    let item = part;
    if (part.media?.items?.some(media => !media.media_id)) {
      const detail = await api(`detail?content_id=${id}`);
      const resolved = detail.parts.find(entry => entry.content_id === id);
      if (!resolved) throw new Error("内容项详情已变化，请重新检索");
      item = { ...resolved, media: { items: resolved.media } };
    }
    setQueue(items => [...items, { ...item, content_id: id, instance: ++serial.current }]);
  }
  function move(from, to) {
    if (to < 0 || to >= queue.length || from === to) return;
    setQueue(items => {
      const updated = [...items];
      const [item] = updated.splice(from, 1);
      updated.splice(to, 0, item);
      return updated;
    });
  }
  async function create() {
    if (!name.trim() || !queue.length) throw new Error("请输入组合名称并至少加入一个内容项");
    const saved = await api("compositions", { name, description, content_ids: queue.map(item => item.content_id) });
    setCreated(saved.composition_id);
    if (names(tags).length) {
      try { await api(`compositions/${saved.composition_id}/tags/add`, { names: names(tags), namespace: "general" }); }
      catch (error) { setWarning(`组合 ${saved.composition_id} 已创建，但标签关联失败：${error.message}。可以重试关联，勿重复创建。`); await refreshDirectory({ propagate: false }); return; }
    }
    setWarning("");
    await refreshDirectory({ propagate: false });
    setSavedDetail(await api(`compositions/${saved.composition_id}`));
  }
  async function refreshDirectory({ propagate = true } = {}) {
    const request = ++directoryRequest.current;
    try {
      const data = await api("compositions?limit=50&offset=0");
      if (active.current && directoryRequest.current === request) {
        setExisting(data); setDirectoryError("");
        submittedDirectory.current = { name: "", id: "", tags: [], memberTags: false };
        setLookupName(""); setLookupId(""); setFilter(""); setMemberTags(false);
      }
    } catch (error) {
      if (active.current && directoryRequest.current === request) {
        setExisting(null); setDirectoryError(error.message);
      }
      if (propagate) throw error;
    }
  }
  async function browse(offset = 0) {
    const submitted = offset ? submittedDirectory.current : { name: lookupName.trim(), id: lookupId.trim(), tags: names(filter), memberTags };
    const params = new URLSearchParams({ limit: "50", offset: String(offset), match_member_tags: String(submitted.memberTags) });
    if (submitted.name) params.set("name", submitted.name);
    const request = ++directoryRequest.current;
    try {
      if (submitted.id) params.set("composition_id", String(parseId(submitted.id)));
      submitted.tags.forEach(tag => params.append("tags", tag));
      const data = await api(`compositions?${params}`);
      if (active.current && directoryRequest.current === request) {
        if (!offset) submittedDirectory.current = submitted;
        setExisting(data); setDirectoryError("");
      }
    } catch (error) {
      if (active.current && directoryRequest.current === request) {
        setExisting(null); setDirectoryError(error.message);
      }
      throw error;
    }
  }
  async function retryTags() {
    if (!created || !names(tags).length) return;
    await api(`compositions/${created}/tags/add`, { names: names(tags), namespace: "general" });
    setWarning("");
    await refreshDirectory({ propagate: false });
    setSavedDetail(await api(`compositions/${created}`));
  }

  return <div className="page compositions-page">
    <PageHeader eyebrow="COMPOSITIONS / 阅读路径" title="组合书架" description="组合不是文件夹，而是一条有顺序的阅读路径。成员可以来自不同消息，也可以重复出现；组合自身的标签与成员标签彼此独立。" actions={<a className="button button-primary" href="#create-composition"><Icon name="plus" size={16} />创建新组合</a>} />

    <section className="surface directory-panel" aria-label="已有组合目录">
      <div className="section-heading">
        <div><p className="eyebrow">YOUR READING PATHS</p><h3>已有组合</h3></div>
        <p>进入页面即列出最近的组合。可以按 ID、名称或组合自身标签继续查找。</p>
      </div>
      <form className="directory-form" onSubmit={event => { event.preventDefault(); run(() => browse()); }}>
        <label className="field"><span className="field-label">组合 ID</span><input type="number" min="1" step="1" value={lookupId} onChange={event => setLookupId(event.target.value)} placeholder="精确 ID" /></label>
        <label className="field"><span className="field-label">组合名称包含</span><input value={lookupName} maxLength="255" onChange={event => setLookupName(event.target.value)} placeholder="例如：周末阅读" /></label>
        <label className="field"><span className="field-label">检索组合标签（每行一个）</span><textarea rows="1" value={filter} onChange={event => setFilter(event.target.value)} placeholder="组合自身标签" /></label>
        <label className="checkbox-row directory-check"><input type="checkbox" checked={memberTags} onChange={event => setMemberTags(event.target.checked)} />同时匹配可见成员标签</label>
        <button type="submit" className="button button-ghost"><Icon name="search" size={15} />查找组合</button>
      </form>
      {directoryError && <p className="inline-notice tone-danger" role="alert"><Icon name="warning" size={16} /><span>{directoryError}</span><button type="button" className="text-button" onClick={() => run(refreshDirectory)}>重试读取组合</button></p>}
      {existing ? <div className="directory-body">
        <div className="directory-actions"><p>共 {existing.total} 个组合</p><a className="quiet-link" href="#create-composition">跳到创建新组合 <Icon name="arrowDown" size={14} /></a></div>
        {existing.total === 0 && <EmptyState icon="layers" title="未找到符合条件的组合"><p>调整筛选条件，或从下方创建第一条阅读路径。</p></EmptyState>}
        <ul className="composition-results" tabIndex="0" aria-label="组合检索结果">{existing.compositions.map(item => <li key={item.composition_id}>
          <div className="composition-card">
            <div className="composition-card-head">
              <div><span className="composition-serial">COMPOSITION {String(item.composition_id).padStart(2, "0")}</span><strong>{item.name}（组合 {item.composition_id}）</strong></div>
              <div className="composition-card-actions"><Link className="button button-small button-ghost" to={`/compositions/${item.composition_id}`}>管理组合 {item.composition_id}</Link><Link className="quiet-link" to={`/model?composition_id=${item.composition_id}`}>用此组合测试模型建议</Link></div>
            </div>
            <div className="composition-own-tags"><span>组合自身标签</span><TagList tags={item.tags || []} empty={<span className="muted">无自身标签</span>} /><small>不传播给成员</small></div>
            <ol className="composition-mini-sequence">{item.members.map(member => <li key={member.position}>
              <span className="mini-position">{String(member.position + 1).padStart(2, "0")}</span>
              <span>{member.available ? <><TypeMark type={member.item_type} compact /> <span className="mini-text">{compactText(member.text || member.original_name, 64) || `内容项 ${member.content_id}`}</span></> : "不可用位置"}</span>
            </li>)}</ol>
          </div>
        </li>)}</ul>
        <div className="result-footer"><button className="button button-ghost" disabled={!existing.has_more} onClick={() => run(() => browse(existing.offset + existing.limit))}>下一页组合 <Icon name="arrowRight" size={15} /></button><span>{existing.has_more ? "还有更多组合" : `已显示 ${existing.compositions.length} 个`}</span></div>
      </div> : !directoryError && <div className="loading-lines" aria-label="正在读取组合"><span /><span /><span /></div>}
    </section>

    <section className="surface builder-panel" id="create-composition">
      <div className="section-heading">
        <div><p className="eyebrow">BUILD A READING PATH</p><h3>创建新组合</h3></div>
        <p>左侧挑选内容项，右侧按阅读顺序排列。一个消息里的正文和图片可以分别选择；重复加入同一个内容项是被允许的。</p>
      </div>
      <div className="composition-layout">
        <section className="builder-candidates" aria-label="检索和勾选区">
          <div className="builder-section-head"><span className="step-badge">01</span><div><h3>检索和勾选</h3><p>先找到内容项，再把它们加入右侧路径。</p></div></div>
          <form className="candidate-search" onSubmit={event => { event.preventDefault(); run(() => search()); }}>
            <label className="search-input"><Icon name="search" size={17} /><span className="sr-only">内容项关键词</span><input aria-label="内容项关键词" value={term} onChange={event => setTerm(event.target.value)} maxLength="4096" placeholder="检索要加入的内容项…" /></label>
            <button type="submit" className="button button-ghost">检索内容项</button>
          </form>
          <p className="candidate-summary">{result ? `第 ${result.page} 页，本页 ${result.hits.length} 条消息` : "正在读取内容…"}</p>
          <div className="candidate-list">
            {(result?.hits || []).map(hit => <article className="candidate-message" key={hit.message_id}>
              <div className="candidate-message-head"><span>消息 {hit.message_id}</span><span>{hit.parts?.length || 0} 个可见内容项</span></div>
              {(hit.parts || []).map(part => <div className="composition-candidate" key={part.content_id}>
                <label className="candidate-check"><input type="checkbox" aria-label={`加入内容项 ${part.content_id}`} checked={queue.some(item => item.content_id === part.content_id)} onChange={event => { if (event.target.checked) run(() => add(part)); else setQueue(items => items.filter(item => item.content_id !== part.content_id)); }} /><span className="check-visual"><Icon name="check" size={12} /></span><span><TypeMark type={part.item_type} compact /> 内容项 {part.content_id}</span></label>
                {part.text && <p className="composition-preview">{part.text}</p>}
                <CandidateImages part={part} />
              </div>)}
            </article>)}
          </div>
          <div className="result-footer"><button type="button" className="button button-ghost" disabled={!result?.has_more} onClick={() => run(() => search(true))}>下一页内容 <Icon name="arrowRight" size={15} /></button></div>
        </section>

        <section className="builder-queue" aria-label="组合顺序和确认区">
          <div className="builder-section-head"><span className="step-badge">02</span><div><h3>组合顺序和确认</h3><p>拖放、上移或下移调整位置。每个位置只引用一次，不会删除原内容。</p></div><span className="queue-count">{queue.length} 个位置</span></div>
          {!queue.length && <EmptyState icon="layers" title="阅读路径还是空的"><p>从左侧勾选内容项后，它们会按加入顺序出现在这里。</p></EmptyState>}
          <ol className="composition-queue">{queue.map((item, index) => <li key={item.instance} draggable onDragStart={() => { dragged.current = index; }} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (dragged.current !== null) move(dragged.current, index); dragged.current = null; }} onDragEnd={() => { dragged.current = null; }}>
            <span className="queue-handle"><Icon name="drag" size={16} /></span>
            <span className="queue-index">{String(index + 1).padStart(2, "0")}</span>
            <div className="queue-copy"><div className="queue-meta"><TypeMark type={item.item_type} compact /><span>内容项 {item.content_id}</span></div>{item.text && <p className="composition-preview">{item.text}</p>}<QueueImages item={item} /></div>
            <div className="queue-actions"><button type="button" className="icon-button" disabled={index === 0} aria-label={`上移位置 ${index + 1} 的内容项 ${item.content_id}`} onClick={() => move(index, index - 1)}><Icon name="arrowUp" size={15} /></button><button type="button" className="icon-button" disabled={index === queue.length - 1} aria-label={`下移位置 ${index + 1} 的内容项 ${item.content_id}`} onClick={() => move(index, index + 1)}><Icon name="arrowDown" size={15} /></button><button type="button" className="icon-button" aria-label={`再次加入位置 ${index + 1} 的内容项 ${item.content_id}`} onClick={() => run(() => add(item))}><Icon name="plus" size={15} /></button><button type="button" className="icon-button is-danger" aria-label={`移除位置 ${index + 1} 的内容项 ${item.content_id}`} onClick={() => setQueue(items => items.filter(entry => entry.instance !== item.instance))}><Icon name="close" size={15} /></button></div>
          </li>)}</ol>

          <div className="composition-meta-form">
            <label className="field"><span className="field-label">组合名称</span><input value={name} maxLength="255" onChange={event => setName(event.target.value)} placeholder="为这条阅读路径命名" /></label>
            <label className="field"><span className="field-label">组合说明</span><textarea value={description} onChange={event => setDescription(event.target.value)} placeholder="它为什么值得按这个顺序读？" /></label>
            <label className="field"><span className="field-label">组合标签（每行一个）</span><textarea aria-label="组合标签（每行一个）" value={tags} onChange={event => setTags(event.target.value)} placeholder={"慢读\n设计"} /><span className="field-help">组合自身标签，不复制给成员。</span></label>
            <button type="button" className="button button-primary button-wide" disabled={!name.trim() || !queue.length || Boolean(created)} onClick={() => run(create)}><Icon name="layers" size={16} />确认构建组合</button>
          </div>
          {created && <div className="created-receipt"><span className="receipt-mark"><Icon name="check" size={18} /></span><div><strong>组合 {created} 已创建</strong><p>这条阅读路径已经写入组合书架。</p><div className="action-row"><Link className="text-link" to={`/compositions/${created}`}>管理组合 {created} <Icon name="arrowRight" size={14} /></Link><Link className="text-link" to={`/model?composition_id=${created}`}>用此组合测试模型建议 <Icon name="arrowRight" size={14} /></Link></div></div></div>}
          {savedDetail && <p className="saved-sequence">保存顺序：{savedDetail.members.map(member => member.available ? `内容项 ${member.content_id}` : `位置 ${member.position} 不可用`).join(" → ")}；自身标签：{savedDetail.tags.map(tag => tag.name).join("、") || "无"}</p>}
          {warning && <p role="alert" className="inline-notice tone-danger"><Icon name="warning" size={16} /><span>{warning}</span>{created && names(tags).length > 0 && <button className="text-button" onClick={() => run(retryTags)}>重试关联标签</button>}</p>}
        </section>
      </div>
    </section>
  </div>;
}
