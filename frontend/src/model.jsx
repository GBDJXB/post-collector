import React, { useEffect, useState } from "react";
import { useLocation } from "react-router";

// 两种 API 分别返回对象和 JSON 字符串；坏值不参与证明，也不影响列表渲染。
function parseEvidence(value) {
  if (typeof value === "string") {
    try { value = JSON.parse(value); }
    catch { return null; }
  }
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function readableEvidence(value) {
  const parsed = parseEvidence(value);
  return parsed ? JSON.stringify(parsed) : "证据格式不可用";
}

// 只使用当前画面中已加载的证明；确认时不重新从服务端补齐版本。
export function proposalProofs(rows, ids) {
  return Object.fromEntries(rows.filter(row => ids.includes(row.assign_id)).flatMap(row => {
    const evidence = parseEvidence(row.evidence);
    return evidence?.model_proposal ? [[row.assign_id, evidence.model_proposal]] : [];
  }));
}

export function ModelPage({ api, run, parseId }) {
  const location = useLocation();
  const linkedId = new URLSearchParams(location.search).get("composition_id");
  const [status, setStatus] = useState(null), [key, setKey] = useState("");
  const [enableConsent, setEnableConsent] = useState(false), [sendConsent, setSendConsent] = useState(false);
  const [kind, setKind] = useState(linkedId ? "composition_id" : "content_id"), [id, setId] = useState(linkedId || "");
  const [preview, setPreview] = useState(null), [outcome, setOutcome] = useState(null);
  const [assignments, setAssignments] = useState([]), [selected, setSelected] = useState([]);
  const [composition, setComposition] = useState(null), [compositions, setCompositions] = useState([]), [error, setError] = useState("");
  useEffect(() => { let active = true; api("model/status").then(value => { if (active) setStatus(value); }).catch(() => { if (active) setError("模型状态不可用"); }); return () => { active = false; }; }, [api]);
  const target = () => ({ [kind]: parseId(id) });
  function reset() { setPreview(null); setOutcome(null); setSendConsent(false); setAssignments([]); setSelected([]); setComposition(null); }
  async function setCredential() {
    const value = key; setKey(""); reset(); setEnableConsent(false);
    setStatus(await api("model/key/set", { key: value }));
  }
  async function access(enabled) {
    reset(); setEnableConsent(false);
    setStatus(await api("model/access", { enabled, confirmed: enabled ? enableConsent : true }));
  }
  async function loadSuggestions() {
    const ident = parseId(id);
    setSelected([]);
    const url = kind === "composition_id" ? `compositions/${ident}/assignments` : `assignments?content_ids=${ident}`;
    setAssignments((await api(url)).assignments);
  }
  async function review(confirm) {
    if (!selected.length) throw new Error("请勾选模型建议");
    const path = kind === "composition_id" ? `compositions/${parseId(id)}/suggestions` : "suggestions";
    await api(`${path}/${confirm ? "confirm" : "reject"}`, { ids: selected, expected_runs: proposalProofs(assignments, selected) });
    await loadSuggestions();
  }
  async function send() {
    const ident = preview.preview_id;
    setPreview(null); setSendConsent(false); setOutcome("等待结果；请勿重复发送");
    try { const result = await api("model/send", { preview_id: ident, confirmed: true }); setOutcome(`${result.status}（请求 ${result.request_id}）${result.detail ? `：${result.detail}` : result.error_class ? `：${result.error_class}` : ""}`); }
    catch (error) {
      setOutcome(error.status >= 400 && error.status < 500
        ? "未外发：请求在预留前被拒绝，本次不会计费"
        : "结果未知，可能计费；不会自动重试。请检查服务记录");
      throw error;
    }
  }
  return <><section><h2>模型标签建议</h2><p>只选择已有标签，至少一个；不会创建标签，不提供自然语言检索。建议不生效，须由 root 确认。</p>
    <p>密钥仅保存在服务进程内，不写入数据库、日志或浏览器存储。替换、删除、退出、会话失效或重启后须重新设置并开启。不能保证物理擦除。</p>
    <p>{error || (status ? `内存密钥：${status.key_present ? "已设置" : "未设置"}；外发总闸：${status.enabled ? "开启" : "关闭"}` : "读取模型状态…")}</p>
    <form onSubmit={event => { event.preventDefault(); run(setCredential); }}><label>模型 API Key <input type="password" autoComplete="off" maxLength="4096" value={key} onChange={event => setKey(event.target.value)} /></label><button type="submit" disabled={!key}>设置内存密钥</button></form>
    <button onClick={() => run(async () => { setKey(""); reset(); setEnableConsent(false); setStatus(await api("model/key/delete", {})); })}>删除内存密钥</button>
    <label><input type="checkbox" checked={enableConsent} onChange={event => setEnableConsent(event.target.checked)} />我确认开启模型外发总闸</label>
    <button disabled={!enableConsent || !status?.key_present} onClick={() => run(() => access(true))}>开启模型外发</button><button onClick={() => run(() => access(false))}>关闭模型外发并清除密钥</button>
  </section><section><h2>一个内容项或独立组合</h2>
    <label>模型目标类型 <select value={kind} onChange={event => { reset(); setKind(event.target.value); }}><option value="content_id">单个内容项</option><option value="composition_id">独立有序组合</option></select></label>
    <label>模型目标 ID <input type="number" min="1" value={id} onChange={event => { reset(); setId(event.target.value); }} /></label>
    <button onClick={() => run(async () => { reset(); setPreview(await api("model/preview", target())); })}>生成外发预览</button>
    <button onClick={() => run(loadSuggestions)}>查看模型目标建议</button>
    {kind === "composition_id" && <><button onClick={() => run(async () => setCompositions((await api("compositions")).compositions))}>列出已有组合</button><button onClick={() => run(async () => setComposition(await api(`compositions/${parseId(id)}`)))}>查看组合顺序与自身标签</button><ul>{compositions.map(item => <li key={item.composition_id}><button onClick={() => { reset(); setId(String(item.composition_id)); }}>{item.composition_id}：{item.name}</button></li>)}</ul></>}
    {composition && <article><h3>组合 {composition.composition_id}：{composition.name}</h3><p>自身标签：{composition.tags.map(tag => tag.name).join("、") || "无"}；模型建议不传播到成员。</p>{composition.members.map(member => <div key={member.position}><p>位置 {member.position}：{member.available ? `内容 ${member.content_id} (${member.item_type})` : "不可用"}</p>{member.available && <><pre>{member.text}</pre>{member.media.map(media => <img key={media.media_id} alt={`位置 ${member.position} 的图片`} loading="lazy" src={`/api/v1/media/${member.content_id}/${media.media_id}`} />)}</>}</div>)}</article>}
    {preview && <article><h3>本次外发预览</h3><p>{preview.provider} / {preview.model}；{preview.endpoint}；预览 {preview.expires_in_seconds} 秒有效。</p><p>输入 {preview.request_bytes} 字节；候选 {preview.candidate_count} 个；滚动24小时剩余 {preview.attempts_remaining} 次。</p><p>{preview.metadata_warning}</p><p>{preview.catalog_warning}</p><p>{preview.unknown_outcome_warning}</p><p>上限：20位置、4次图片、单图5 MiB、图片总计10 MiB、文本32 KiB UTF-8、请求16 MiB、输出2048 tokens、响应256 KiB。单进程仅一个请求、不排队。</p>
      {preview.parts.map(part => <div key={part.position}><p>位置 {part.position}：{part.item_type}</p><pre>{part.text}</pre>{part.images.map((image, index) => <p key={index}>原始图片 {index + 1}：{image.mime}，{image.size_bytes} 字节；{image.dimensions_verified ? `${image.width}×${image.height}` : "尺寸未知，未验证像素限制"}</p>)}</div>)}
      <h4>完整候选目录</h4><ol>{preview.candidate_catalog.map(tag => <li key={tag.candidate_id}>{tag.name} ({tag.namespace})</li>)}</ol>
      <label><input type="checkbox" checked={sendConsent} onChange={event => setSendConsent(event.target.checked)} />我同意将本次预览发送给模型</label><button disabled={!sendConsent} onClick={() => run(send)}>发送本次预览</button></article>}
    {outcome && <p>模型结果：{outcome}</p>}
    <h3>待审核关联</h3><ul>{assignments.map(row => <li key={row.assign_id}>{row.source === "suggested" && !row.revoked_at && <input type="checkbox" aria-label={`模型选择建议 ${row.assign_id}`} checked={selected.includes(row.assign_id)} onChange={event => setSelected(event.target.checked ? [...selected, row.assign_id] : selected.filter(value => value !== row.assign_id))} />}{row.name}（{row.source}，置信度 {row.confidence}）{row.revoked_at ? "，已撤销" : ""}{row.evidence && <pre>{readableEvidence(row.evidence)}</pre>}</li>)}</ul>
    <button onClick={() => run(() => review(true))}>确认模型建议</button><button onClick={() => run(() => review(false))}>拒绝模型建议</button>
  </section></>;
}
