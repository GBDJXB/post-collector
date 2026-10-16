import React, { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { LinkedText, safeWebUrl } from "./links.jsx";

const tagNames = value => value.split(/\r?\n/).map(name => name.trim()).filter(Boolean);

export function CompositionDetailPage({ api, run, parseId }) {
  const { composition_id: rawId } = useParams();
  let id;
  try { id = parseId(rawId); }
  catch { return <section><p role="alert">组合 ID 无效。</p><Link to="/compositions">返回组合目录</Link></section>; }
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

  if (deleted) return <section><h2>组合 {id} 已软删除</h2><p>原内容和其他组合不受影响。</p><Link to="/compositions">返回组合目录</Link></section>;
  return <section className="composition-editor"><h2>管理组合 {id}</h2><p><Link to="/compositions">返回组合目录</Link> · <Link to={`/model?composition_id=${id}`}>用此组合测试模型建议</Link></p>
    {error && <p role="alert">{error} <button type="button" onClick={() => run(refresh)}>刷新组合详情</button></p>}
    {!detail ? !error && <p>正在读取组合…</p> : <>
      <p>只解除所选位置的一次引用，不删除原内容或其他组合中的引用。不可用成员保留位置且不展示内容 ID。</p>
      <section><h3>名称与说明</h3><form onSubmit={event => { event.preventDefault(); run(() => change("update", { name, description, expected_updated_at: detail.updated_at })); }}>
        <label>组合名称 <input value={name} maxLength="255" required onChange={event => setName(event.target.value)} /></label>
        <label>组合说明 <textarea value={description} onChange={event => setDescription(event.target.value)} /></label>
        <button type="submit">保存组合属性</button></form></section>
      <section><h3>成员顺序</h3>{!detail.members.length && <p>这个组合目前没有成员。</p>}
        <ol className="composition-members" aria-label="组合成员顺序" tabIndex={0}>{detail.members.map((member, index) => <li key={member.position}>
          {member.available ? <><span>内容项 {member.content_id}（{member.item_type}）</span>{member.text && <p className="composition-preview"><LinkedText text={member.text} /></p>}{member.url && safeWebUrl(member.url) && <a href={safeWebUrl(member.url)} target="_blank" rel="noopener noreferrer">{member.url}</a>}{(member.media || []).filter(media => media.mime_detected?.startsWith("image/")).map(media => <img className="composition-image" key={media.media_id} src={`/api/v1/media/${member.content_id}/${media.media_id}`} alt={`位置 ${index + 1} 的图片`} loading="lazy" />)}</> : <span>位置 {index + 1}：不可用</span>}
          <div><button type="button" disabled={index === 0} aria-label={`上移位置 ${index + 1}`} onClick={() => run(() => memberChange("move", { position: index, to_position: index - 1 }))}>上移</button>
          <button type="button" disabled={index === detail.members.length - 1} aria-label={`下移位置 ${index + 1}`} onClick={() => run(() => memberChange("move", { position: index, to_position: index + 1 }))}>下移</button>
          <button type="button" aria-label={`移除位置 ${index + 1}`} onClick={() => remove(index)}>移除</button></div>
        </li>)}</ol>
        {forcePosition !== null && <div role="group" aria-label="最后成员强制确认"><p>移除最后成员会保留一个空组合；这不是删除原内容。确认强制移除位置 {forcePosition + 1}？</p>
          <button type="button" onClick={() => run(() => memberChange("remove", { position: forcePosition, force_empty: true }))}>确认强制移除最后成员</button>
          <button type="button" onClick={() => setForcePosition(null)}>取消强制移除</button></div>}
      </section>
      <section><h3>加入可见内容</h3><form onSubmit={event => { event.preventDefault(); run(() => search()); }}>
        <label>内容项关键词 <input value={term} maxLength="4096" onChange={event => setTerm(event.target.value)} /></label><button type="submit">检索可加入内容</button></form>
        {candidateError && <p role="alert">{candidateError} <button type="button" onClick={() => run(() => search())}>重试检索</button></p>}
        {(candidates?.hits || []).map(hit => <article key={hit.message_id}><h4>消息 {hit.message_id}</h4>{(hit.parts || []).map(part => <div className="composition-candidate" key={part.content_id}><span>内容项 {part.content_id}（{part.item_type}）</span>{part.text && <p className="composition-preview">{part.text}</p>}
          <button type="button" aria-label={`加入内容项 ${part.content_id}`} onClick={() => run(() => memberChange("add", { content_id: parseId(part.content_id) }))}>加入</button></div>)}</article>)}
        {candidates && <button type="button" disabled={!candidates.has_more} onClick={() => run(() => search(true))}>下一页内容</button>}
      </section>
      <section><h3>组合自身标签</h3><ul>{detail.tags.map(tag => <li key={`${tag.namespace}:${tag.name}`}>{tag.name}（{tag.namespace}） <button type="button" aria-label={`撤销组合标签 ${tag.name}`} onClick={() => run(() => change("tags/remove", { names: [tag.name], namespace: tag.namespace }))}>撤销</button></li>)}</ul>
        <label>新增自身标签 <textarea value={tags} onChange={event => setTags(event.target.value)} /></label>
        <button type="button" disabled={!tagNames(tags).length} onClick={() => run(async () => { await change("tags/add", { names: tagNames(tags), namespace: "general" }); setTags(""); })}>添加组合标签</button></section>
      <section><h3>删除组合</h3><p>仅软删除组合；不会删除原内容和媒体。</p>
        {!confirmDelete ? <button type="button" onClick={() => setConfirmDelete(true)}>软删除组合</button> : <div role="group" aria-label="删除组合确认"><p>确认软删除组合 {id}？</p><button type="button" onClick={() => run(deleteComposition)}>确认删除组合</button><button type="button" onClick={() => setConfirmDelete(false)}>取消删除</button></div>}</section>
    </>}
  </section>;
}
