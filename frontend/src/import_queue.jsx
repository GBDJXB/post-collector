import React, { useRef, useState } from "react";
import { Link } from "react-router";
import { buildUploadForm, changeQueuedImage, queuedImages, compositionMembers, editable, expectedPartCount, groupQueuedMessage, messages, moveQueuedUnit, readyForComposition, removeQueuedMessage } from "./import_queue.js";

const summary = item => [item.draft.text, ...item.draft.entries.map(entry => entry.text || entry.url || entry.file?.name)].filter(Boolean).join(" · ");
const statusText = item => ({ pending: "待入库", uploading: "上传中", succeeded: "已入库", failed: "失败", unresolved: "结果不确定，请人工核对", terminal: item.result?.status || "无法完成" })[item.status];

function MessageResult({ item }) {
  return <>{item.result?.detail} {item.result?.duplicate && "已存在；"}{item.result?.content_ids?.length > 0 && item.result?.message_id && <Link to={`/manage?message_id=${item.result.message_id}`}>查看消息 {item.result.message_id}</Link>}</>;
}

export function ImportQueue({ api, run, busy, queue, setQueue, results, setResults, refresh }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [confirmRemove, setConfirmRemove] = useState(null);
  const dragged = useRef(null);
  const processing = useRef(false);
  const groups = queue.filter(unit => unit.kind === "group");
  const all = messages(queue);
  const available = all.filter(item => editable(item) && !groups.some(group => group.members.includes(item) && !editable(group)));

  function createGroup() {
    if (!name.trim() || name.length > 255 || /\p{Cc}/u.test(name)) throw new Error("组合名称须为非空单行文本（最多 255 字符）");
    const id = available.some(item => item.id === selected) ? selected : available[0]?.id;
    if (!id) throw new Error("请先暂存一条消息");
    setQueue(current => groupQueuedMessage(current, id, name.trim(), description));
    setName(""); setDescription(""); setSelected("");
  }
  function move(id, groupId, beforeId) {
    if (!busy && !processing.current) setQueue(current => moveQueuedUnit(current, id, groupId, beforeId));
  }
  function dropProps(groupId, beforeId) {
    return { onDragOver: event => { event.preventDefault(); event.stopPropagation(); }, onDrop: event => {
      event.preventDefault(); event.stopPropagation();
      if (dragged.current) move(dragged.current, groupId, beforeId);
      dragged.current = null;
    } };
  }
  function dragProps(unit, allowed) {
    return { draggable: allowed && !busy, onDragStart: event => {
      event.stopPropagation();
      if (!allowed || busy || processing.current) { event.preventDefault(); return; }
      dragged.current = unit.id;
      event.dataTransfer.setData("text/plain", unit.id);
      event.dataTransfer.effectAllowed = "move";
    }, onDragEnd: () => { dragged.current = null; } };
  }
  function arrows(unit, list, index, groupId = null) {
    return <><button type="button" aria-label={`上移${unit.kind === "group" ? "组合 " + unit.name : "暂存消息 " + (all.indexOf(unit) + 1)}`} disabled={index === 0} onClick={() => move(unit.id, groupId, list[index - 1].id)}>上移</button>
      <button type="button" aria-label={`下移${unit.kind === "group" ? "组合 " + unit.name : "暂存消息 " + (all.indexOf(unit) + 1)}`} disabled={index === list.length - 1} onClick={() => move(unit.id, groupId, list[index + 2]?.id || null)}>下移</button></>;
  }
  function messageRow(item, list, index, group = null) {
    const number = all.indexOf(item) + 1;
    const images = queuedImages(item.draft);
    const allowed = editable(item) && (!group || editable(group));
    return <li key={item.id} {...dragProps(item, allowed)} {...dropProps(group?.id || null, item.id)}>
      <span className="summary-clamp">消息 {number}：{summary(item)} · {group?.name || "独立消息"} · {statusText(item)}</span>
      {images.length > 0 && <div><p>已暂存图片：{images.length} 张</p>
        <ul aria-label={`消息 ${number} 的暂存图片`}>{images.map((file, position) => <li key={position}>{file.name}（{file.size} 字节）{allowed && <>
          <button type="button" aria-label={`分离消息 ${number} 的图片 ${position + 1}`} disabled={expectedPartCount(item.draft) <= 1} onClick={() => setQueue(current => changeQueuedImage(current, item.id, position, true))}>分离为独立消息</button>
          <button type="button" aria-label={`删除消息 ${number} 的图片 ${position + 1}`} onClick={() => setQueue(current => changeQueuedImage(current, item.id, position))}>删除图片</button>
        </>}</li>)}</ul>
      </div>}
      {item.draft.note && <p className="summary-clamp">备注：{item.draft.note}</p>}
      {allowed && <>{arrows(item, list, index, group?.id)}<label>消息 {number} 所属组合 <select value={group?.id || ""} onChange={event => move(item.id, event.target.value || null)}>
        <option value="">独立消息</option>{groups.filter(editable).map(target => <option key={target.id} value={target.id}>{target.name}</option>)}
      </select></label><button type="button" onClick={() => setQueue(current => removeQueuedMessage(current, item.id))}>移除暂存消息 {number}</button></>}
      {["failed", "unresolved", "terminal"].includes(item.status) && !group?.uncertain && <>{confirmRemove === item.id ? <span>移除前请核对：消息可能已经归档；移除暂存记录不会删除归档。<button type="button" onClick={() => { setQueue(current => removeQueuedMessage(current, item.id)); setConfirmRemove(null); }}>确认移除失败消息 {number}</button><button type="button" onClick={() => setConfirmRemove(null)}>取消移除</button></span> : <button type="button" onClick={() => setConfirmRemove(item.id)}>移除失败消息 {number}</button>}</>}
      {item.error && <p>{item.error}</p>}<MessageResult item={item} />
    </li>;
  }

  async function process() {
    if (processing.current || !queue.length) return;
    processing.current = true; setError("");
    let working = queue;
    let changed = false;
    const publish = next => { working = next; setQueue(next); };
    const updateMessage = (id, patch) => publish(working.map(unit => unit.kind === "group" ? { ...unit, members: unit.members.map(item => item.id === id ? { ...item, ...patch } : item) } : unit.id === id ? { ...unit, ...patch } : unit));
    const updateGroup = (id, patch) => publish(working.map(unit => unit.id === id ? { ...unit, ...patch } : unit));
    try {
      const oversized = groups.find(group => group.members.reduce((total, item) => total + (item.result?.content_ids?.length || expectedPartCount(item.draft)), 0) > 500);
      if (oversized) throw new Error(`组合 ${oversized.name} 超过 500 个成员，请在入库前调整暂存消息`);
      const failures = [];
      for (const unit of queue) {
        for (const item of unit.kind === "group" ? unit.members : [unit]) {
          if (item.status === "succeeded") continue;
          if (item.status !== "pending") { failures.push(`消息无法自动完成：${item.result?.status || item.error || "结果不确定，请人工核对"}`); continue; }
          updateMessage(item.id, { status: "uploading" });
          let result;
          try { changed = true; result = await api("uploads", buildUploadForm(item.draft)); }
          catch (cause) {
            const definite = cause.status >= 400 && cause.status < 500;
            updateMessage(item.id, { status: definite ? "failed" : "unresolved", error: cause.message });
            if (!definite || cause.status === 401) throw cause;
            failures.push(cause.message); continue;
          }
          const status = result.status === "succeeded" && result.content_ids?.length > 0 ? "succeeded"
            : ["quarantined", "dead", "succeeded"].includes(result.status) ? "terminal" : "unresolved";
          updateMessage(item.id, { status, result });
          if (status !== "succeeded") failures.push(`消息无法自动完成：${result.status || "结果未知"}；请人工核对`);
        }
        if (unit.kind !== "group" || unit.compositionId) continue;
        if (unit.uncertain) { failures.push("创建结果不确定，请到组合目录核对，勿自动重试"); continue; }
        if (!readyForComposition(working, unit.id)) continue;
        try {
          changed = true;
          const result = await api("compositions", { name: unit.name, description: unit.description, content_ids: compositionMembers(working, unit.id) });
          if (!Number.isSafeInteger(result.composition_id) || result.composition_id <= 0) throw new Error("组合创建结果缺少确认 ID");
          updateGroup(unit.id, { compositionId: result.composition_id, failure: null });
        } catch (cause) {
          const definite = cause.status >= 400 && cause.status < 500;
          updateGroup(unit.id, { uncertain: !definite, failure: definite ? cause.message : null });
          if (!definite || cause.status === 401) throw cause;
          failures.push(cause.message);
        }
      }
      if (failures.length) throw new Error(failures.join("；"));
      const complete = working.every(unit => unit.kind === "group" ? Boolean(unit.compositionId) : unit.status === "succeeded");
      if (!complete) throw new Error("仍有未完成项，请核对结果后处理");
      setResults(working); setQueue([]);
    } catch (cause) { setError(cause.message); throw cause; }
    finally {
      processing.current = false;
      // 刷新失败不能覆盖已保存的入库结果，也不能诱发重发。
      if (changed) await refresh();
    }
  }

  return <section aria-label="待导入消息队列"><h3>2. 编排</h3>
    <p>拖动列表单元调整入库顺序；拖到消息前插入，拖到组合末尾加入。也可用上移、下移和所属组合选择框。空组合自动消失。</p>
    <label>新组合名称 <input value={name} maxLength="255" onChange={event => setName(event.target.value)} /></label>
    <label>新组合说明 <textarea value={description} onChange={event => setDescription(event.target.value)} /></label>
    <label>用哪条消息创建组合 <select value={available.some(item => item.id === selected) ? selected : available[0]?.id || ""} onChange={event => setSelected(event.target.value)}>
      {!available.length && <option value="">请先暂存消息</option>}{available.map(item => <option key={item.id} value={item.id}>{summary(item).slice(0, 80)}</option>)}
    </select></label><button type="button" disabled={!available.length} onClick={() => run(createGroup)}>创建暂存组合</button>
    <div className="table-wrap"><ol className="composition-queue" aria-label="待导入消息">{queue.map((unit, index) => unit.kind !== "group" ? messageRow(unit, queue, index) : <li key={unit.id} {...dragProps(unit, editable(unit))}>
      <div {...dropProps(null, unit.id)}>插入到组合 {unit.name} 之前</div>
      <h4>{unit.name} · {unit.members.length} 条消息</h4>{editable(unit) && arrows(unit, queue, index)}
      {!unit.compositionId && !unit.uncertain && <><label>{unit.name}的名称 <input maxLength="255" value={unit.name} onChange={event => setQueue(current => current.map(item => item.id === unit.id ? { ...item, name: event.target.value } : item))} /></label>
        <label>{unit.name}的说明 <textarea value={unit.description} onChange={event => setQueue(current => current.map(item => item.id === unit.id ? { ...item, description: event.target.value } : item))} /></label></>}
      <ol className="composition-queue">{unit.members.map((item, position) => messageRow(item, unit.members, position, unit))}</ol>
      {editable(unit) && <p {...dropProps(unit.id, null)}>拖到此处加入组合 {unit.name} 的末尾</p>}
      {unit.compositionId ? <Link to={`/compositions/${unit.compositionId}`}>组合 {unit.compositionId}</Link> : unit.uncertain ? <p>创建结果不确定，请到组合目录核对，勿自动重试</p> : unit.failure && <p>{unit.failure}</p>}
    </li>)}</ol><p {...dropProps(null, null)}>拖到此处成为列表末尾的独立消息或组合</p></div>
    <h3>3. 入库</h3><p>按列表顺序逐项入库；组合先上传全部成员，再创建组合。不同请求不保证原子提交。失败或未知结果保留，已成功项不重发。组合标签在入库后的组合详情页添加。</p>
    {error && <p role="alert">{error}</p>}<button type="button" disabled={!queue.length} onClick={() => run(process)}>全部入库</button>
    {results.length > 0 && <section aria-label="入库结果"><h4>入库结果</h4>{results.map(unit => <div key={unit.id}>{(unit.kind === "group" ? unit.members : [unit]).map(item => <p key={item.id}><MessageResult item={item} /></p>)}{unit.kind === "group" && <p><Link to={`/compositions/${unit.compositionId}`}>组合 {unit.compositionId}</Link> · 在详情页添加组合标签、调整成员</p>}</div>)}</section>}
  </section>;
}
