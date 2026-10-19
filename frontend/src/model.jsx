import React, { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";
import { compactText, EmptyState, Icon, InlineNotice, PageHeader, TagList, TypeMark } from "./ui.jsx";

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
    catch (caught) {
      setOutcome(caught.status >= 400 && caught.status < 500
        ? "未外发：请求在预留前被拒绝，本次不会计费"
        : "结果未知，可能计费；不会自动重试。请检查服务记录");
      throw caught;
    }
  }

  return <div className="page model-page">
    <PageHeader eyebrow="ASSISTED TAGGING / 模型辅助" title="模型标签建议" description="模型只从已有标签中选择建议，不会创建标签，也不会替代你的判断。每一条建议都需要 root 明确确认后才会生效。" actions={<Link className="button button-ghost" to="/manage"><Icon name="library" size={16} />返回归档</Link>} />
    {error && <InlineNotice tone="danger">{error}</InlineNotice>}
    <section className="surface model-gate">
      <div className="section-heading"><div><p className="eyebrow">LOCAL SESSION & EGRESS</p><h2>外发闸门</h2></div><span className={`gate-status${status?.enabled ? " is-open" : ""}`}><span className="status-dot" />{status ? (status.enabled ? "外发已开启" : "外发已关闭") : "读取状态…"}</span></div>
      <div className="gate-grid">
        <div className="gate-copy">
          <p>密钥仅保存在服务进程内，不写入数据库、日志或浏览器存储。替换、删除、退出、会话失效或重启后须重新设置并开启。程序不能保证物理内存擦除。</p>
          <p>{status ? `内存密钥：${status.key_present ? "已设置" : "未设置"}；外发总闸：${status.enabled ? "开启" : "关闭"}` : "正在读取模型状态…"}</p>
        </div>
        <div className="gate-actions">
          <form onSubmit={event => { event.preventDefault(); run(setCredential); }}><label className="field"><span className="field-label">模型 API Key</span><input type="password" autoComplete="off" maxLength="4096" value={key} onChange={event => setKey(event.target.value)} placeholder="仅在当前进程内使用" /></label><button type="submit" className="button button-ghost" disabled={!key}><Icon name="key" size={15} />设置内存密钥</button></form>
          <div className="action-row"><button className="button button-small button-ghost" onClick={() => run(async () => { setKey(""); reset(); setEnableConsent(false); setStatus(await api("model/key/delete", {})); })}>删除内存密钥</button><button className="button button-small button-ghost" onClick={() => run(() => access(false))}>关闭模型外发并清除密钥</button></div>
          <label className="checkbox-row"><input type="checkbox" checked={enableConsent} onChange={event => setEnableConsent(event.target.checked)} />我确认开启模型外发总闸</label>
          <button className="button button-primary" disabled={!enableConsent || !status?.key_present} onClick={() => run(() => access(true))}><Icon name="shield" size={15} />开启模型外发</button>
        </div>
      </div>
    </section>

    <div className="model-grid">
      <section className="surface model-target">
        <div className="section-heading"><div><p className="eyebrow">TARGET</p><h2>一个内容项或独立组合</h2></div></div>
        <p className="section-intro">组合会保持成员顺序和重复次数；组合建议不会传播给成员。存在隐藏或不可用成员时，整个组合不会被发送。</p>
        <label className="field"><span className="field-label">模型目标类型</span><select value={kind} onChange={event => { reset(); setKind(event.target.value); }}><option value="content_id">单个内容项</option><option value="composition_id">独立有序组合</option></select></label>
        <label className="field"><span className="field-label">模型目标 ID</span><input type="number" min="1" value={id} onChange={event => { reset(); setId(event.target.value); }} /></label>
        <div className="action-row"><button className="button button-primary" onClick={() => run(async () => { reset(); setPreview(await api("model/preview", target())); })}><Icon name="sparkles" size={15} />生成外发预览</button><button className="button button-ghost" onClick={() => run(loadSuggestions)}>查看模型目标建议</button></div>
        {kind === "composition_id" && <div className="model-composition-tools"><div className="action-row"><button className="button button-small button-ghost" onClick={() => run(async () => setCompositions((await api("compositions")).compositions))}>列出已有组合</button><button className="button button-small button-ghost" onClick={() => run(async () => setComposition(await api(`compositions/${parseId(id)}`)))}>查看组合顺序与自身标签</button></div><ul className="model-composition-list">{compositions.map(item => <li key={item.composition_id}><button className="text-button" onClick={() => { reset(); setId(String(item.composition_id)); }}>{item.composition_id}：{item.name}</button></li>)}</ul></div>}
        {composition && <article className="model-composition-preview"><h3>组合 {composition.composition_id}：{composition.name}</h3><div className="own-tags-block"><span className="own-tags-label">自身标签</span><TagList tags={composition.tags} empty={<span className="muted">无</span>} /><small>模型建议不传播到成员。</small></div>{composition.members.map(member => <div className="model-member" key={member.position}><p>位置 {member.position + 1}：{member.available ? `内容 ${member.content_id} (${member.item_type})` : "不可用"}</p>{member.available && <><pre>{member.text}</pre>{member.media.map(media => <img key={media.media_id} alt={`位置 ${member.position + 1} 的图片`} loading="lazy" src={`/api/v1/media/${member.content_id}/${media.media_id}`} />)}</>}</div>)}</article>}
      </section>

      <section className="surface model-preview-panel">
        <div className="section-heading"><div><p className="eyebrow">PREVIEW BEFORE SEND</p><h2>外发预览</h2></div></div>
        {!preview && <EmptyState icon="sparkles" title="先预览，再决定"><p>预览会列出将发送的正文、图片、候选标签和限额。只有勾选逐次同意后才会真正发送。</p></EmptyState>}
        {preview && <article className="preview-card">
          <h3>本次外发预览</h3><p className="preview-endpoint">{preview.provider} / {preview.model}<br /><span>{preview.endpoint}</span></p>
          <dl className="mini-stats"><div><dt>有效期</dt><dd>{preview.expires_in_seconds} 秒</dd></div><div><dt>输入</dt><dd>{preview.request_bytes} 字节</dd></div><div><dt>候选</dt><dd>{preview.candidate_count} 个</dd></div><div><dt>剩余</dt><dd>{preview.attempts_remaining} 次</dd></div></dl>
          <div className="preview-warnings"><p>{preview.metadata_warning}</p><p>{preview.catalog_warning}</p><p>{preview.unknown_outcome_warning}</p></div>
          <p className="field-help">上限：20 位置、4 次图片、单图 5 MiB、图片总计 10 MiB、文本 32 KiB UTF-8、请求 16 MiB、输出 2048 tokens、响应 256 KiB。单进程仅一个请求、不排队。</p>
          <div className="preview-parts">{preview.parts.map(part => <div key={part.position} className="preview-part"><p className="eyebrow">位置 {part.position + 1} · {part.item_type}</p><pre>{part.text}</pre>{part.images.map((image, index) => <p key={index} className="preview-image-note">原始图片 {index + 1}：{image.mime}，{image.size_bytes} 字节；{image.dimensions_verified ? `${image.width}×${image.height}` : "尺寸未知，未验证像素限制"}</p>)}</div>)}</div>
          <div className="candidate-catalog"><h4>完整候选目录</h4><ol>{preview.candidate_catalog.map(tag => <li key={tag.candidate_id}>{tag.name} ({tag.namespace})</li>)}</ol></div>
          <label className="checkbox-row consent-row"><input type="checkbox" checked={sendConsent} onChange={event => setSendConsent(event.target.checked)} />我同意将本次预览发送给模型</label>
          <button className="button button-primary button-wide" disabled={!sendConsent} onClick={() => run(send)}><Icon name="arrowRight" size={16} />发送本次预览</button>
        </article>}
        {outcome && <p className="model-outcome">模型结果：{outcome}</p>}
      </section>
    </div>

    <section className="surface review-panel">
      <div className="section-heading"><div><p className="eyebrow">HUMAN REVIEW</p><h2>待审核关联</h2></div><p>建议不会自动生效。确认会生成人工关联；拒绝只撤销建议。</p></div>
      {!assignments.length && <EmptyState icon="tag" title="还没有载入建议"><p>选择目标后点击“查看模型目标建议”。</p></EmptyState>}
      <ul className="review-list">{assignments.map(row => <li key={row.assign_id} className={row.revoked_at ? "is-revoked" : ""}>{row.source === "suggested" && !row.revoked_at && <input type="checkbox" aria-label={`模型选择建议 ${row.assign_id}`} checked={selected.includes(row.assign_id)} onChange={event => setSelected(event.target.checked ? [...selected, row.assign_id] : selected.filter(value => value !== row.assign_id))} />}<div className="review-copy"><div className="review-title"><TagList tags={[{ name: row.name }]} /><span className="source-pill">{row.source}</span><span className="confidence">置信度 {row.confidence}</span>{row.revoked_at && <span className="muted">已撤销</span>}</div>{row.evidence && <pre>{readableEvidence(row.evidence)}</pre>}</div></li>)}</ul>
      <div className="action-row"><button className="button button-primary" onClick={() => run(() => review(true))}>确认模型建议</button><button className="button button-ghost" onClick={() => run(() => review(false))}>拒绝模型建议</button></div>
    </section>
  </div>;
}
