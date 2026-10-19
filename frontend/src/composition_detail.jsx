import React, { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { LinkedText, safeWebUrl } from "./links.jsx";
import { compactText, EmptyState, formatDate, Icon, InlineNotice, PageHeader, TagList, TagPill, TypeMark } from "./ui.jsx";

const tagNames = value => value.split(/\r?\n/).map(name => name.trim()).filter(Boolean);

export function CompositionDetailPage({ api, run, parseId }) {
  const { composition_id: rawId } = useParams();
  let id;
  try { id = parseId(rawId); }
  catch { return <div className="page"><EmptyState icon="warning" title="组合 ID 无效" action={<Link className="button button-primary" to="/compositions">返回组合目录</Link>}>请检查地址中的组合 ID。</EmptyState></div>; }
  return <CompositionEditor key={id} id={id} api={api} run={run} parseId={parseId} />;
}

function CompositionEditor({ id, api, run, parseId }) {
  const [detail, setDetail] = useState(null), [error, setError] = useState("");
  const [name, setName] = useState(""), [description, setDescription] = useState("");
  const [tags, setTags] = useState(""), [term, setTerm] = useState("");
  const [candidates, setCandidates] = useState(null), [candidateError, setCandidateError] = useState("");
  const [forcePosition, setForcePosition] = useState(null), [confirmDelete, setConfirmDelete] = useState(false);
  const [deleted, setDeleted] = useState(false);
  const active = useRef(true), request = useRef(0), candidateRequest = useRef(0), submittedTerm = useRef("");
  const base = `compositions/${id}`;

  async function refresh() {
    const serial = ++request.current;
    try {
      const data = await api(base);
      if (active.current && serial === request.current) {
        setDetail(data); setName(data.name); setDescription(data.description); setError("");
        setForcePosition(null); setConfirmDelete(false);
      }
    } catch (caught) {
      if (active.current && serial === request.current) setError(caught.message);
      throw caught;
    }
  }
  useEffect(() => {
    active.current = true;
    refresh().catch(() => {});
    return () => { active.current = false; ++request.current; ++candidateRequest.current; };
  }, [api, id]);

  async function change(path, body) {
    try {
      await api(`${base}/${path}`, body);
      await refresh();
      setForcePosition(null);
    } catch (caught) {
      if (active.current) setError(caught.message);
      throw caught;
    }
  }
  function memberChange(operation, values) {
    return change(`members/${operation}`, { ...values, expected_updated_at: detail.updated_at });
  }
  async function search(next = false) {
    const serial = ++candidateRequest.current;
    const params = new URLSearchParams({ sort: "time", limit: "40", page: String(next ? candidates.page + 1 : 1) });
    const query = next ? submittedTerm.current : term.trim();
    if (query) params.set("q", query);
    if (next && candidates.next_cursor) params.set("cursor", candidates.next_cursor);
    try {
      const found = await api(`search?${params}`);
      if (active.current && serial === candidateRequest.current) {
        if (!next) submittedTerm.current = query;
        setCandidates(found); setCandidateError("");
      }
    } catch (caught) {
      if (active.current && serial === candidateRequest.current) setCandidateError(caught.message);
      throw caught;
    }
  }
  function remove(position) {
    if (detail.members.length === 1) { setForcePosition(position); return; }
    run(() => memberChange("remove", { position, force_empty: false }));
  }
  async function deleteComposition() {
    try {
      await api(`${base}/delete`, { expected_updated_at: detail.updated_at });
      setDeleted(true); setDetail(null);
    } catch (caught) {
      if (active.current) setError(caught.message);
      throw caught;
    }
  }

  if (deleted) return <div className="page"><EmptyState icon="archive" title={`组合 ${id} 已软删除`} action={<Link className="button button-primary" to="/compositions">返回组合目录</Link>}>原内容和其他组合不受影响。</EmptyState></div>;
  const visibleMembers = detail?.members.filter(member => member.available) || [];
  const uniqueMembers = new Set(visibleMembers.map(member => member.content_id)).size;
  const repeated = visibleMembers.length - uniqueMembers;
  return <div className="page composition-editor-page">
    <PageHeader eyebrow={`COMPOSITION ${String(id).padStart(2, "0")} / 独立组合`} title={`管理组合 ${id}`} description="组合保存的是一条有顺序的阅读路径。成员引用与重复次数独立于原内容，组合自身标签也不会传播。" actions={<><Link className="button button-ghost" to="/compositions"><Icon name="chevronLeft" size={15} />返回组合目录</Link><Link className="button button-ghost" to={`/model?composition_id=${id}`}><Icon name="sparkles" size={15} />测试模型建议</Link></>} />
    {error && <p role="alert" className="inline-notice tone-danger"><Icon name="warning" size={16} /><span>{error}</span><button type="button" className="text-button" onClick={() => run(refresh)}>刷新组合详情</button></p>}
    {!detail ? !error && <section className="surface"><div className="loading-lines" aria-label="正在读取组合"><span /><span /><span /></div></section> : <div className="composition-editor-grid">
      <div className="composition-editor-main">
        <section className="surface sequence-editor">
          <div className="section-heading"><div><p className="eyebrow">SEQUENCE</p><h2>成员顺序</h2></div><div className="sequence-stats"><span><strong>{detail.members.length}</strong> 个位置</span><span><strong>{uniqueMembers}</strong> 个不同内容项</span>{repeated > 0 && <span><strong>{repeated}</strong> 次重复引用</span>}</div></div>
          <p className="section-intro">每个位置只解除一次引用；上移/下移会保留其他人的相对顺序。不可用成员保留原位置，不展示内容 ID。</p>
          {!detail.members.length && <EmptyState icon="layers" title="这个组合目前没有成员。"><p>它仍保留名称和自身标签，可以从下方重新加入可见内容。</p></EmptyState>}
          <ol className="composition-members" aria-label="组合成员顺序" tabIndex={0}>{detail.members.map((member, index) => <li key={member.position} className={member.available ? "" : "is-unavailable"}>
            <span className="member-position">{String(index + 1).padStart(2, "0")}</span>
            <div className="member-body">
              {member.available ? <><div className="member-meta"><TypeMark type={member.item_type} compact /><span>内容项 {member.content_id}（{member.item_type}）</span></div>{member.text && <p className="composition-preview"><LinkedText text={member.text} /></p>}{member.url && safeWebUrl(member.url) && <a className="reader-link" href={safeWebUrl(member.url)} target="_blank" rel="noopener noreferrer"><Icon name="link" size={14} />{member.url}</a>}{(member.media || []).filter(media => media.mime_detected?.startsWith("image/")).map(media => <img className="composition-image" key={media.media_id} src={`/api/v1/media/${member.content_id}/${media.media_id}`} alt={`位置 ${index + 1} 的图片`} loading="lazy" />)}</> : <span>位置 {index + 1}：不可用</span>}
            </div>
            <div className="member-actions"><button type="button" className="icon-button" disabled={index === 0} aria-label={`上移位置 ${index + 1}`} onClick={() => run(() => memberChange("move", { position: index, to_position: index - 1 }))}><Icon name="arrowUp" size={15} /></button><button type="button" className="icon-button" disabled={index === detail.members.length - 1} aria-label={`下移位置 ${index + 1}`} onClick={() => run(() => memberChange("move", { position: index, to_position: index + 1 }))}><Icon name="arrowDown" size={15} /></button><button type="button" className="icon-button is-danger" aria-label={`移除位置 ${index + 1}`} onClick={() => remove(index)}><Icon name="close" size={15} /></button></div>
          </li>)}</ol>
          {forcePosition !== null && <div role="group" aria-label="最后成员强制确认" className="confirm-box"><p>移除最后成员会保留一个空组合；这不是删除原内容。确认强制移除位置 {forcePosition + 1}？</p><div className="action-row"><button type="button" className="button button-small button-danger" onClick={() => run(() => memberChange("remove", { position: forcePosition, force_empty: true }))}>确认强制移除最后成员</button><button type="button" className="button button-small button-ghost" onClick={() => setForcePosition(null)}>取消强制移除</button></div></div>}
        </section>

        <section className="surface add-member-panel">
          <div className="section-heading"><div><p className="eyebrow">EXTEND THE PATH</p><h2>加入可见内容</h2></div><span className="result-mode">加入后位于末尾</span></div>
          <form className="candidate-search" onSubmit={event => { event.preventDefault(); run(() => search()); }}><label className="search-input"><Icon name="search" size={17} /><span className="sr-only">内容项关键词</span><input aria-label="内容项关键词" value={term} maxLength="4096" onChange={event => setTerm(event.target.value)} placeholder="检索可加入的内容项…" /></label><button type="submit" className="button button-ghost">检索可加入内容</button></form>
          {candidateError && <p role="alert" className="inline-notice tone-danger"><Icon name="warning" size={16} /><span>{candidateError}</span><button type="button" className="text-button" onClick={() => run(() => search())}>重试检索</button></p>}
          <div className="candidate-list">{(candidates?.hits || []).map(hit => <article className="candidate-message" key={hit.message_id}><div className="candidate-message-head"><span>消息 {hit.message_id}</span></div>{(hit.parts || []).map(part => <div className="composition-candidate" key={part.content_id}><div className="candidate-add-row"><span><TypeMark type={part.item_type} compact /> 内容项 {part.content_id}</span><button type="button" className="button button-small button-ghost" aria-label={`加入内容项 ${part.content_id}`} onClick={() => run(() => memberChange("add", { content_id: parseId(part.content_id) }))}><Icon name="plus" size={14} />加入</button></div>{part.text && <p className="composition-preview">{part.text}</p>}</div>)}</article>)}</div>
          {candidates && <div className="result-footer"><button type="button" className="button button-ghost" disabled={!candidates.has_more} onClick={() => run(() => search(true))}>下一页内容 <Icon name="arrowRight" size={15} /></button></div>}
        </section>
      </div>

      <aside className="composition-editor-aside">
        <section className="surface side-card">
          <div className="section-heading"><div><p className="eyebrow">PROPERTIES</p><h2>名称与说明</h2></div></div>
          <form onSubmit={event => { event.preventDefault(); run(() => change("update", { name, description, expected_updated_at: detail.updated_at })); }}>
            <label className="field"><span className="field-label">组合名称</span><input value={name} maxLength="255" required onChange={event => setName(event.target.value)} /></label>
            <label className="field"><span className="field-label">组合说明</span><textarea value={description} onChange={event => setDescription(event.target.value)} /></label>
            <button type="submit" className="button button-primary button-wide">保存组合属性</button>
          </form>
          <p className="field-help">创建于 {formatDate(detail.created_at, true)} · 更新于 {formatDate(detail.updated_at, true)}</p>
        </section>

        <section className="surface side-card own-tags-card">
          <div className="section-heading"><div><p className="eyebrow">COMPOSITION TAGS</p><h2>组合自身标签</h2></div></div>
          <p className="section-intro">这些标签只属于组合，不计入成员标签，也不会传播到成员。</p>
          <TagList tags={detail.tags || []} onRemove={tag => run(() => change("tags/remove", { names: [tag.name], namespace: tag.namespace }))} removeLabel={tag => `撤销组合标签 ${tag.name}`} empty={<span className="muted">还没有组合标签</span>} />
          <label className="field"><span className="field-label">新增自身标签</span><textarea value={tags} onChange={event => setTags(event.target.value)} placeholder={"每行一个\n例如：慢读"} /></label>
          <button type="button" className="button button-ghost button-wide" disabled={!tagNames(tags).length} onClick={() => run(async () => { await change("tags/add", { names: tagNames(tags), namespace: "general" }); setTags(""); })}>添加组合标签</button>
        </section>

        <section className="surface side-card danger-card">
          <div className="section-heading"><div><p className="eyebrow">ARCHIVE CONTROL</p><h2>删除组合</h2></div></div>
          <p>仅软删除组合；不会删除原内容、媒体或其他组合中的引用。</p>
          {!confirmDelete ? <button type="button" className="button button-ghost button-danger button-wide" onClick={() => setConfirmDelete(true)}><Icon name="trash" size={15} />软删除组合</button> : <div role="group" aria-label="删除组合确认" className="confirm-box"><p>确认软删除组合 {id}？</p><div className="action-row"><button type="button" className="button button-small button-danger" onClick={() => run(deleteComposition)}>确认删除组合</button><button type="button" className="button button-small button-ghost" onClick={() => setConfirmDelete(false)}>取消删除</button></div></div>}
        </section>
      </aside>
    </div>}
  </div>;
}
