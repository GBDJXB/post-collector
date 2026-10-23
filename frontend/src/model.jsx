import React, { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";
import { localizedError, useI18n } from "./i18n/index.jsx";
import { EmptyState, Icon, InlineNotice, PageHeader, TagList } from "./ui.jsx";

// 两种 API 分别返回对象和 JSON 字符串；坏值不参与证明，也不影响列表渲染。
function parseEvidence(value) {
  if (typeof value === "string") {
    try { value = JSON.parse(value); }
    catch { return null; }
  }
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function readableEvidence(value, t) {
  const parsed = parseEvidence(value);
  return parsed ? JSON.stringify(parsed) : t("model.evidenceUnreadable");
}

// 只使用当前画面中已加载的证明；确认时不重新从服务端补齐版本。
export function proposalProofs(rows, ids) {
  return Object.fromEntries(rows.filter(row => ids.includes(row.assign_id)).flatMap(row => {
    const evidence = parseEvidence(row.evidence);
    return evidence?.model_proposal ? [[row.assign_id, evidence.model_proposal]] : [];
  }));
}

function resultLine(result, t) {
  const head = t("model.outcome.line", { status: result.status, request: result.request_id });
  if (result.detail) return head + t("model.outcome.detailSuffix", { detail: result.detail });
  if (result.error_class) return head + t("model.outcome.classSuffix", { errorClass: result.error_class });
  return head;
}

export function ModelPage({ api, run, parseId }) {
  const { t } = useI18n();
  const location = useLocation();
  const linkedId = new URLSearchParams(location.search).get("composition_id");
  const [status, setStatus] = useState(null), [key, setKey] = useState("");
  const [enableConsent, setEnableConsent] = useState(false), [sendConsent, setSendConsent] = useState(false);
  const [kind, setKind] = useState(linkedId ? "composition_id" : "content_id"), [id, setId] = useState(linkedId || "");
  const [preview, setPreview] = useState(null), [outcome, setOutcome] = useState(null);
  const [assignments, setAssignments] = useState([]), [selected, setSelected] = useState([]);
  const [composition, setComposition] = useState(null), [compositions, setCompositions] = useState([]), [error, setError] = useState(null);
  useEffect(() => { let active = true; api("model/status").then(value => { if (active) setStatus(value); }).catch(() => { if (active) setError({ key: "model.error.statusUnavailable" }); }); return () => { active = false; }; }, [api]);
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
    if (!selected.length) throw localizedError("model.selectSuggestions");
    const path = kind === "composition_id" ? `compositions/${parseId(id)}/suggestions` : "suggestions";
    await api(`${path}/${confirm ? "confirm" : "reject"}`, { ids: selected, expected_runs: proposalProofs(assignments, selected) });
    await loadSuggestions();
  }
  async function send() {
    const ident = preview.preview_id;
    setPreview(null); setSendConsent(false); setOutcome({ type: "key", key: "model.outcome.waiting" });
    try {
      const result = await api("model/send", { preview_id: ident, confirmed: true });
      setOutcome({ type: "result", result });
    } catch (caught) {
      setOutcome({ type: "key", key: caught.status >= 400 && caught.status < 500 ? "model.outcome.rejected" : "model.outcome.unknown" });
      throw caught;
    }
  }
  const outcomeText = !outcome ? "" : t("model.outcome.result", { result: outcome.type === "result" ? resultLine(outcome.result, t) : t(outcome.key) });

  return <div className="page model-page">
    <PageHeader eyebrow={t("model.eyebrow")} title={t("model.title")} description={t("model.description")} actions={<Link className="button button-ghost" to="/manage"><Icon name="library" size={16} />{t("model.backToArchive")}</Link>} />
    {error && <InlineNotice tone="danger">{t(error.key)}</InlineNotice>}
    <section className="surface model-gate">
      <div className="section-heading"><div><p className="eyebrow">{t("model.gateEyebrow")}</p><h2>{t("model.gateHeading")}</h2></div><span className={`gate-status${status?.enabled ? " is-open" : ""}`}><span className="status-dot" />{status ? (status.enabled ? t("model.gateOpen") : t("model.gateClosed")) : t("model.loadingStatus")}</span></div>
      <div className="gate-grid">
        <div className="gate-copy">
          <p>{t("model.gateIntro")}</p>
          <p>{status ? t("model.gateState", { key: status.key_present ? t("model.keySet") : t("model.keyUnset"), gate: status.enabled ? t("model.gateOn") : t("model.gateOff") }) : t("model.loadingState")}</p>
        </div>
        <div className="gate-actions">
          <form onSubmit={event => { event.preventDefault(); run(setCredential); }}><label className="field"><span className="field-label">{t("model.keyLabel")}</span><input type="password" autoComplete="off" maxLength="4096" value={key} onChange={event => setKey(event.target.value)} placeholder={t("model.keyPlaceholder")} /></label><button type="submit" className="button button-ghost" disabled={!key}><Icon name="key" size={15} />{t("model.setKey")}</button></form>
          <div className="action-row"><button className="button button-small button-ghost" onClick={() => run(async () => { setKey(""); reset(); setEnableConsent(false); setStatus(await api("model/key/delete", {})); })}>{t("model.deleteKey")}</button><button className="button button-small button-ghost" onClick={() => run(() => access(false))}>{t("model.disableEgress")}</button></div>
          <label className="checkbox-row"><input type="checkbox" checked={enableConsent} onChange={event => setEnableConsent(event.target.checked)} />{t("model.enableConsent")}</label>
          <button className="button button-primary" disabled={!enableConsent || !status?.key_present} onClick={() => run(() => access(true))}><Icon name="shield" size={15} />{t("model.enableEgress")}</button>
        </div>
      </div>
    </section>

    <div className="model-grid">
      <section className="surface model-target">
        <div className="section-heading"><div><p className="eyebrow">{t("model.targetEyebrow")}</p><h2>{t("model.targetHeading")}</h2></div></div>
        <p className="section-intro">{t("model.targetIntro")}</p>
        <label className="field"><span className="field-label">{t("model.targetKind")}</span><select value={kind} onChange={event => { reset(); setKind(event.target.value); }}><option value="content_id">{t("model.targetContent")}</option><option value="composition_id">{t("model.targetComposition")}</option></select></label>
        <label className="field"><span className="field-label">{t("model.targetId")}</span><input type="number" min="1" value={id} onChange={event => { reset(); setId(event.target.value); }} /></label>
        <div className="action-row"><button className="button button-primary" onClick={() => run(async () => { reset(); setPreview(await api("model/preview", target())); })}><Icon name="sparkles" size={15} />{t("model.previewAction")}</button><button className="button button-ghost" onClick={() => run(loadSuggestions)}>{t("model.viewSuggestions")}</button></div>
        {kind === "composition_id" && <div className="model-composition-tools"><div className="action-row"><button className="button button-small button-ghost" onClick={() => run(async () => setCompositions((await api("compositions")).compositions))}>{t("model.listCompositions")}</button><button className="button button-small button-ghost" onClick={() => run(async () => setComposition(await api(`compositions/${parseId(id)}`)))}>{t("model.showComposition")}</button></div><ul className="model-composition-list">{compositions.map(item => <li key={item.composition_id}><button className="text-button" onClick={() => { reset(); setId(String(item.composition_id)); }}>{t("model.compositionOption", { id: item.composition_id, name: item.name })}</button></li>)}</ul></div>}
        {composition && <article className="model-composition-preview"><h3>{t("model.compositionTitle", { id: composition.composition_id, name: composition.name })}</h3><div className="own-tags-block"><span className="own-tags-label">{t("model.ownTags")}</span><TagList tags={composition.tags} empty={<span className="muted">{t("model.none")}</span>} /><small>{t("model.ownTagsNote")}</small></div>{composition.members.map(member => <div className="model-member" key={member.position}><p>{t("model.memberPosition", { position: member.position + 1, content: member.available ? t("model.memberContent", { id: member.content_id, type: member.item_type }) : t("model.unavailable") })}</p>{member.available && <><pre>{member.text}</pre>{member.media.map(media => <img key={media.media_id} alt={t("model.memberImageAlt", { position: member.position + 1 })} loading="lazy" src={`/api/v1/media/${member.content_id}/${media.media_id}`} />)}</>}</div>)}</article>}
      </section>

      <section className="surface model-preview-panel">
        <div className="section-heading"><div><p className="eyebrow">{t("model.previewEyebrow")}</p><h2>{t("model.previewHeading")}</h2></div></div>
        {!preview && <EmptyState icon="sparkles" title={t("model.previewEmptyTitle")}><p>{t("model.previewEmptyBody")}</p></EmptyState>}
        {preview && <article className="preview-card">
          <h3>{t("model.previewTitle")}</h3><p className="preview-endpoint">{preview.provider} / {preview.model}<br /><span>{preview.endpoint}</span></p>
          <dl className="mini-stats"><div><dt>{t("model.statExpires")}</dt><dd>{t("model.statSeconds", { value: preview.expires_in_seconds, count: preview.expires_in_seconds })}</dd></div><div><dt>{t("model.statInput")}</dt><dd>{t("model.statBytes", { value: preview.request_bytes, count: preview.request_bytes })}</dd></div><div><dt>{t("model.statCandidates")}</dt><dd>{t("model.statCount", { value: preview.candidate_count, count: preview.candidate_count })}</dd></div><div><dt>{t("model.statRemaining")}</dt><dd>{t("model.statAttempts", { value: preview.attempts_remaining, count: preview.attempts_remaining })}</dd></div></dl>
          <div className="preview-warnings"><p>{preview.metadata_warning}</p><p>{preview.catalog_warning}</p><p>{preview.unknown_outcome_warning}</p></div>
          <p className="field-help">{t("model.limitsHelp")}</p>
          <div className="preview-parts">{preview.parts.map(part => <div key={part.position} className="preview-part"><p className="eyebrow">{t("model.partPosition", { position: part.position + 1, type: part.item_type })}</p><pre>{part.text}</pre>{part.images.map((image, index) => <p key={index} className="preview-image-note">{t("model.imageNote", { index: index + 1, mime: image.mime, size: image.size_bytes, dimensions: image.dimensions_verified ? `${image.width}×${image.height}` : t("model.dimensionsUnknown") })}</p>)}</div>)}</div>
          <div className="candidate-catalog"><h4>{t("model.catalogHeading")}</h4><ol>{preview.candidate_catalog.map(tag => <li key={tag.candidate_id}>{tag.name} ({tag.namespace})</li>)}</ol></div>
          <label className="checkbox-row consent-row"><input type="checkbox" checked={sendConsent} onChange={event => setSendConsent(event.target.checked)} />{t("model.sendConsent")}</label>
          <button className="button button-primary button-wide" disabled={!sendConsent} onClick={() => run(send)}><Icon name="arrowRight" size={16} />{t("model.sendAction")}</button>
        </article>}
        {outcomeText && <p className="model-outcome">{outcomeText}</p>}
      </section>
    </div>

    <section className="surface review-panel">
      <div className="section-heading"><div><p className="eyebrow">{t("model.reviewEyebrow")}</p><h2>{t("model.reviewHeading")}</h2></div><p>{t("model.reviewIntro")}</p></div>
      {!assignments.length && <EmptyState icon="tag" title={t("model.reviewEmptyTitle")}><p>{t("model.reviewEmptyBody")}</p></EmptyState>}
      <ul className="review-list">{assignments.map(row => <li key={row.assign_id} className={row.revoked_at ? "is-revoked" : ""}>{row.source === "suggested" && !row.revoked_at && <input type="checkbox" aria-label={t("model.selectSuggestion", { id: row.assign_id })} checked={selected.includes(row.assign_id)} onChange={event => setSelected(event.target.checked ? [...selected, row.assign_id] : selected.filter(value => value !== row.assign_id))} />}<div className="review-copy"><div className="review-title"><TagList tags={[{ name: row.name }]} /><span className="source-pill">{row.source}</span><span className="confidence">{t("model.confidence", { value: row.confidence })}</span>{row.revoked_at && <span className="muted">{t("model.revoked")}</span>}</div>{row.evidence && <pre>{readableEvidence(row.evidence, t)}</pre>}</div></li>)}</ul>
      <div className="action-row"><button className="button button-primary" onClick={() => run(() => review(true))}>{t("model.confirmSuggestions")}</button><button className="button button-ghost" onClick={() => run(() => review(false))}>{t("model.rejectSuggestions")}</button></div>
    </section>
  </div>;
}
