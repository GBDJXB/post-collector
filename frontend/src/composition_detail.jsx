import React, { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { describeError, useI18n } from "./i18n/index.jsx";
import { LinkedText, safeWebUrl } from "./links.jsx";
import { EmptyState, formatDate, Icon, PageHeader, TagList, TypeMark } from "./ui.jsx";

const tagNames = value => value.split(/\r?\n/).map(name => name.trim()).filter(Boolean);

export function CompositionDetailPage({ api, run, parseId }) {
  const { t } = useI18n();
  const { composition_id: rawId } = useParams();
  let id;
  try { id = parseId(rawId); }
  catch { return <div className="page"><EmptyState icon="warning" title={t("composition.invalidTitle")} action={<Link className="button button-primary" to="/compositions">{t("composition.backToDirectory")}</Link>}>{t("composition.invalidBody")}</EmptyState></div>; }
  return <CompositionEditor key={id} id={id} api={api} run={run} parseId={parseId} />;
}

function CompositionEditor({ id, api, run, parseId }) {
  const { t, lang } = useI18n();
  const [detail, setDetail] = useState(null), [error, setError] = useState(null);
  const [name, setName] = useState(""), [description, setDescription] = useState("");
  const [tags, setTags] = useState(""), [term, setTerm] = useState("");
  const [candidates, setCandidates] = useState(null), [candidateError, setCandidateError] = useState(null);
  const [forcePosition, setForcePosition] = useState(null), [confirmDelete, setConfirmDelete] = useState(false);
  const [deleted, setDeleted] = useState(false);
  const active = useRef(true), request = useRef(0), candidateRequest = useRef(0), submittedTerm = useRef("");
  const base = `compositions/${id}`;

  async function refresh() {
    const serial = ++request.current;
    try {
      const data = await api(base);
      if (active.current && serial === request.current) {
        setDetail(data); setName(data.name); setDescription(data.description); setError(null);
        setForcePosition(null); setConfirmDelete(false);
      }
    } catch (caught) {
      if (active.current && serial === request.current) setError(caught);
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
      if (active.current) setError(caught);
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
        setCandidates(found); setCandidateError(null);
      }
    } catch (caught) {
      if (active.current && serial === candidateRequest.current) setCandidateError(caught);
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
      if (active.current) setError(caught);
      throw caught;
    }
  }

  if (deleted) return <div className="page"><EmptyState icon="archive" title={t("composition.deletedTitle", { id })} action={<Link className="button button-primary" to="/compositions">{t("composition.backToDirectory")}</Link>}>{t("composition.deletedBody")}</EmptyState></div>;
  const visibleMembers = detail?.members.filter(member => member.available) || [];
  const uniqueMembers = new Set(visibleMembers.map(member => member.content_id)).size;
  const repeated = visibleMembers.length - uniqueMembers;
  return <div className="page composition-editor-page">
    <PageHeader eyebrow={t("composition.eyebrow", { id: String(id).padStart(2, "0") })} title={t("composition.title", { id })} description={t("composition.description")} actions={<><Link className="button button-ghost" to="/compositions"><Icon name="chevronLeft" size={15} />{t("composition.backToDirectory")}</Link><Link className="button button-ghost" to={`/model?composition_id=${id}`}><Icon name="sparkles" size={15} />{t("composition.testModel")}</Link></>} />
    {error && <p role="alert" className="inline-notice tone-danger"><Icon name="warning" size={16} /><span>{describeError(error, t)}</span><button type="button" className="text-button" onClick={() => run(refresh)}>{t("composition.refresh")}</button></p>}
    {!detail ? !error && <section className="surface"><div className="loading-lines" aria-label={t("composition.loadingAria")}><span /><span /><span /></div></section> : <div className="composition-editor-grid">
      <div className="composition-editor-main">
        <section className="surface sequence-editor">
          <div className="section-heading"><div><p className="eyebrow">{t("composition.sequenceEyebrow")}</p><h2>{t("composition.membersHeading")}</h2></div><div className="sequence-stats"><span><strong>{detail.members.length}</strong> {t("composition.positionsUnit", { count: detail.members.length })}</span><span><strong>{uniqueMembers}</strong> {t("composition.uniqueItemsUnit", { count: uniqueMembers })}</span>{repeated > 0 && <span><strong>{repeated}</strong> {t("composition.repeatedUnit", { count: repeated })}</span>}</div></div>
          <p className="section-intro">{t("composition.sequenceIntro")}</p>
          {!detail.members.length && <EmptyState icon="layers" title={t("composition.emptyTitle")}><p>{t("composition.emptyBody")}</p></EmptyState>}
          <ol className="composition-members" aria-label={t("composition.membersAria")} tabIndex={0}>{detail.members.map((member, index) => <li key={member.position} className={member.available ? "" : "is-unavailable"}>
            <span className="member-position">{String(index + 1).padStart(2, "0")}</span>
            <div className="member-body">
              {member.available ? <><div className="member-meta"><TypeMark type={member.item_type} compact /><span>{t("composition.memberLabel", { id: member.content_id, type: member.item_type })}</span></div>{member.text && <p className="composition-preview"><LinkedText text={member.text} /></p>}{member.url && safeWebUrl(member.url) && <a className="reader-link" href={safeWebUrl(member.url)} target="_blank" rel="noopener noreferrer"><Icon name="link" size={14} />{member.url}</a>}{(member.media || []).filter(media => media.mime_detected?.startsWith("image/")).map(media => <img className="composition-image" key={media.media_id} src={`/api/v1/media/${member.content_id}/${media.media_id}`} alt={t("composition.memberImageAlt", { position: index + 1 })} loading="lazy" />)}</> : <span>{t("composition.unavailablePosition", { position: index + 1 })}</span>}
            </div>
            <div className="member-actions"><button type="button" className="icon-button" disabled={index === 0} aria-label={t("composition.moveUp", { position: index + 1 })} onClick={() => run(() => memberChange("move", { position: index, to_position: index - 1 }))}><Icon name="arrowUp" size={15} /></button><button type="button" className="icon-button" disabled={index === detail.members.length - 1} aria-label={t("composition.moveDown", { position: index + 1 })} onClick={() => run(() => memberChange("move", { position: index, to_position: index + 1 }))}><Icon name="arrowDown" size={15} /></button><button type="button" className="icon-button is-danger" aria-label={t("composition.removePosition", { position: index + 1 })} onClick={() => remove(index)}><Icon name="close" size={15} /></button></div>
          </li>)}</ol>
          {forcePosition !== null && <div role="group" aria-label={t("composition.forceGroupAria")} className="confirm-box"><p>{t("composition.forcePrompt", { position: forcePosition + 1 })}</p><div className="action-row"><button type="button" className="button button-small button-danger" onClick={() => run(() => memberChange("remove", { position: forcePosition, force_empty: true }))}>{t("composition.forceConfirm")}</button><button type="button" className="button button-small button-ghost" onClick={() => setForcePosition(null)}>{t("composition.forceCancel")}</button></div></div>}
        </section>

        <section className="surface add-member-panel">
          <div className="section-heading"><div><p className="eyebrow">{t("composition.extendEyebrow")}</p><h2>{t("composition.extendHeading")}</h2></div><span className="result-mode">{t("composition.appendNote")}</span></div>
          <form className="candidate-search" onSubmit={event => { event.preventDefault(); run(() => search()); }}><label className="search-input"><Icon name="search" size={17} /><span className="sr-only">{t("composition.keywordAria")}</span><input aria-label={t("composition.keywordAria")} value={term} maxLength="4096" onChange={event => setTerm(event.target.value)} placeholder={t("composition.keywordPlaceholder")} /></label><button type="submit" className="button button-ghost">{t("composition.searchAvailable")}</button></form>
          {candidateError && <p role="alert" className="inline-notice tone-danger"><Icon name="warning" size={16} /><span>{describeError(candidateError, t)}</span><button type="button" className="text-button" onClick={() => run(() => search())}>{t("composition.retrySearch")}</button></p>}
          <div className="candidate-list">{(candidates?.hits || []).map(hit => <article className="candidate-message" key={hit.message_id}><div className="candidate-message-head"><span>{t("composition.message", { id: hit.message_id })}</span></div>{(hit.parts || []).map(part => <div className="composition-candidate" key={part.content_id}><div className="candidate-add-row"><span><TypeMark type={part.item_type} compact /> {t("manage.partLabel", { id: part.content_id, type: part.item_type })}</span><button type="button" className="button button-small button-ghost" aria-label={t("composition.addItem", { id: part.content_id })} onClick={() => run(() => memberChange("add", { content_id: parseId(part.content_id) }))}><Icon name="plus" size={14} />{t("composition.add")}</button></div>{part.text && <p className="composition-preview">{part.text}</p>}</div>)}</article>)}</div>
          {candidates && <div className="result-footer"><button type="button" className="button button-ghost" disabled={!candidates.has_more} onClick={() => run(() => search(true))}>{t("composition.nextPage")} <Icon name="arrowRight" size={15} /></button></div>}
        </section>
      </div>

      <aside className="composition-editor-aside">
        <section className="surface side-card">
          <div className="section-heading"><div><p className="eyebrow">{t("composition.propertiesEyebrow")}</p><h2>{t("composition.propertiesHeading")}</h2></div></div>
          <form onSubmit={event => { event.preventDefault(); run(() => change("update", { name, description, expected_updated_at: detail.updated_at })); }}>
            <label className="field"><span className="field-label">{t("composition.nameLabel")}</span><input value={name} maxLength="255" required onChange={event => setName(event.target.value)} /></label>
            <label className="field"><span className="field-label">{t("composition.descriptionLabel")}</span><textarea value={description} onChange={event => setDescription(event.target.value)} /></label>
            <button type="submit" className="button button-primary button-wide">{t("composition.saveProperties")}</button>
          </form>
          <p className="field-help">{t("composition.createdUpdated", { created: formatDate(detail.created_at, true, lang), updated: formatDate(detail.updated_at, true, lang) })}</p>
        </section>

        <section className="surface side-card own-tags-card">
          <div className="section-heading"><div><p className="eyebrow">{t("composition.tagsEyebrow")}</p><h2>{t("composition.ownTags")}</h2></div></div>
          <p className="section-intro">{t("composition.tagsIntro")}</p>
          <TagList tags={detail.tags || []} onRemove={tag => run(() => change("tags/remove", { names: [tag.name], namespace: tag.namespace }))} removeLabel={tag => t("composition.removeTagLabel", { name: tag.name })} empty={<span className="muted">{t("composition.noTags")}</span>} />
          <label className="field"><span className="field-label">{t("composition.newTagLabel")}</span><textarea value={tags} onChange={event => setTags(event.target.value)} placeholder={t("composition.newTagPlaceholder")} /></label>
          <button type="button" className="button button-ghost button-wide" disabled={!tagNames(tags).length} onClick={() => run(async () => { await change("tags/add", { names: tagNames(tags), namespace: "general" }); setTags(""); })}>{t("composition.addTags")}</button>
        </section>

        <section className="surface side-card danger-card">
          <div className="section-heading"><div><p className="eyebrow">{t("composition.dangerEyebrow")}</p><h2>{t("composition.deleteHeading")}</h2></div></div>
          <p>{t("composition.deleteIntro")}</p>
          {!confirmDelete ? <button type="button" className="button button-ghost button-danger button-wide" onClick={() => setConfirmDelete(true)}><Icon name="trash" size={15} />{t("composition.softDelete")}</button> : <div role="group" aria-label={t("composition.deleteConfirmAria")} className="confirm-box"><p>{t("composition.deleteConfirm", { id })}</p><div className="action-row"><button type="button" className="button button-small button-danger" onClick={() => run(deleteComposition)}>{t("composition.confirmDelete")}</button><button type="button" className="button button-small button-ghost" onClick={() => setConfirmDelete(false)}>{t("composition.cancelDelete")}</button></div></div>}
        </section>
      </aside>
    </div>}
  </div>;
}
