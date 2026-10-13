import React, { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

const names = text => text.split(/\r?\n/).map(value => value.trim()).filter(Boolean);

export function CompositionsPage({ api, run, parseId }) {
  const [term, setTerm] = useState("");
  const [result, setResult] = useState(null);
  const [queue, setQueue] = useState([]);
  const [name, setName] = useState(""), [description, setDescription] = useState(""), [tags, setTags] = useState("");
  const [created, setCreated] = useState(null), [savedDetail, setSavedDetail] = useState(null), [warning, setWarning] = useState("");
  const [filter, setFilter] = useState(""), [memberTags, setMemberTags] = useState(false), [existing, setExisting] = useState(null);
  const serial = useRef(0), dragged = useRef(null), active = useRef(true), submittedTerm = useRef("");
  useEffect(() => {
    active.current = true;
    api("search?sort=time&limit=40&page=1").then(data => { if (active.current) setResult(data); }).catch(error => { if (active.current) setWarning(error.message); });
    return () => { active.current = false; };
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
      catch (error) { setWarning(`组合 ${saved.composition_id} 已创建，但标签关联失败：${error.message}。可以重试关联，勿重复创建。`); return; }
    }
    setWarning("");
    setSavedDetail(await api(`compositions/${saved.composition_id}`));
  }
  async function browse(offset = 0) {
    const params = new URLSearchParams({ limit: "50", offset: String(offset), match_member_tags: String(memberTags) });
    names(filter).forEach(tag => params.append("tags", tag));
    setExisting(await api(`compositions?${params}`));
  }
  async function retryTags() {
    if (!created || !names(tags).length) return;
    await api(`compositions/${created}/tags/add`, { names: names(tags), namespace: "general" });
    setWarning("");
    setSavedDetail(await api(`compositions/${created}`));
  }
  return <section><h2>整理独立组合</h2><p>从左侧选择内容项，右侧按阅读顺序排列；一个消息中的正文和图片可以分别选择。</p>
    <div className="composition-layout"><section aria-label="检索和勾选区"><h3>检索和勾选</h3>
      <form onSubmit={event => { event.preventDefault(); run(() => search()); }}><label>组合关键词 <input value={term} onChange={event => setTerm(event.target.value)} maxLength="4096" /></label><button type="submit">检索内容项</button></form>
      <p>{result ? `第 ${result.page} 页，本页 ${result.hits.length} 条消息` : "正在读取内容…"}</p>
      {(result?.hits || []).map(hit => <article key={hit.message_id}><h4>消息 {hit.message_id}</h4>{(hit.parts || []).map(part => <div className="composition-candidate" key={part.content_id}>
        <label><input type="checkbox" aria-label={`加入内容项 ${part.content_id}`} checked={queue.some(item => item.content_id === part.content_id)} onChange={event => { if (event.target.checked) run(() => add(part)); else setQueue(items => items.filter(item => item.content_id !== part.content_id)); }} />内容项 {part.content_id}（{part.item_type}）</label>
        {part.text && <p className="composition-preview">{part.text}</p>}{part.media?.items?.filter(media => media.media_id && media.mime_detected?.startsWith("image/")).map(media => <img key={media.media_id} className="composition-image" src={`/api/v1/media/${part.content_id}/${media.media_id}`} alt={`内容项 ${part.content_id} 图片`} loading="lazy" />)}
      </div>)}</article>)}
      <button disabled={!result?.has_more} onClick={() => run(() => search(true))}>下一页内容</button>
    </section><section aria-label="组合顺序和确认区"><h3>组合顺序和确认</h3>
      <ol className="composition-queue">{queue.map((item, index) => <li key={item.instance} draggable onDragStart={() => { dragged.current = index; }} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (dragged.current !== null) move(dragged.current, index); dragged.current = null; }} onDragEnd={() => { dragged.current = null; }}>
        <span>{item.item_type} · 内容项 {item.content_id}</span>{item.text && <p className="composition-preview">{item.text}</p>}
        {item.media?.items?.filter(media => media.media_id && media.mime_detected?.startsWith("image/")).map(media => <img key={media.media_id} className="composition-image" src={`/api/v1/media/${item.content_id}/${media.media_id}`} alt={`内容项 ${item.content_id} 图片`} loading="lazy" />)}
        <div><button type="button" disabled={index === 0} aria-label={`上移位置 ${index + 1} 的内容项 ${item.content_id}`} onClick={() => move(index, index - 1)}>上移</button><button type="button" disabled={index === queue.length - 1} aria-label={`下移位置 ${index + 1} 的内容项 ${item.content_id}`} onClick={() => move(index, index + 1)}>下移</button><button type="button" aria-label={`再次加入位置 ${index + 1} 的内容项 ${item.content_id}`} onClick={() => add(item)}>再次加入</button><button type="button" aria-label={`移除位置 ${index + 1} 的内容项 ${item.content_id}`} onClick={() => setQueue(items => items.filter(entry => entry.instance !== item.instance))}>移除</button></div>
      </li>)}</ol>
      <label>组合名称 <input value={name} maxLength="255" onChange={event => setName(event.target.value)} /></label>
      <label>组合说明 <textarea value={description} onChange={event => setDescription(event.target.value)} /></label>
      <label>组合标签（每行一个）<textarea value={tags} onChange={event => setTags(event.target.value)} /></label>
      <button disabled={!name.trim() || !queue.length || Boolean(created)} onClick={() => run(create)}>确认构建组合</button>
      {created && <p>组合 {created} 已创建。<Link to={`/model?composition_id=${created}`}>用此组合测试模型建议</Link></p>}
      {savedDetail && <p>保存顺序：{savedDetail.members.map(member => member.available ? `内容项 ${member.content_id}` : `位置 ${member.position} 不可用`).join(" → ")}；自身标签：{savedDetail.tags.map(tag => tag.name).join("、") || "无"}</p>}
      {warning && <p role="alert">{warning}{created && names(tags).length > 0 && <button onClick={() => run(retryTags)}>重试关联标签</button>}</p>}
    </section></div>
    <section><h3>检索已有组合</h3><form onSubmit={event => { event.preventDefault(); run(() => browse()); }}>
      <label>检索组合标签（每行一个）<textarea value={filter} onChange={event => setFilter(event.target.value)} /></label>
      <label><input type="checkbox" checked={memberTags} onChange={event => setMemberTags(event.target.checked)} />同时匹配可见成员标签</label>
      <button type="submit">检索已有组合</button></form>
      {existing && <><p>共 {existing.total} 个组合</p><ul>{existing.compositions.map(item => <li key={item.composition_id}><strong>{item.name}（组合 {item.composition_id}）</strong>：{item.tags.map(tag => tag.name).join("、") || "无自身标签"}
        <ol>{item.members.map(member => <li key={member.position}>位置 {member.position}：{member.available ? `内容项 ${member.content_id}（${member.item_type}）` : "不可用"}</li>)}</ol>
        <Link to={`/model?composition_id=${item.composition_id}`}>用此组合测试模型建议</Link></li>)}</ul><button disabled={!existing.has_more} onClick={() => run(() => browse(existing.offset + existing.limit))}>下一页组合</button></>}
    </section></section>;
}
