import React, { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { describeError, localizedError, useI18n } from "./i18n/index.jsx";
import { compactText, EmptyState, Icon, PageHeader, TagList, TypeMark } from "./ui.jsx";

const names = text => text.split(/\r?\n/).map(value => value.trim()).filter(Boolean);

function CandidateImages({ part }) {
  const { t } = useI18n();
  return (part.media?.items || []).filter(media => media.media_id && media.mime_detected?.startsWith("image/")).map(media => <img key={media.media_id} className="composition-image" src={`/api/v1/media/${part.content_id}/${media.media_id}`} alt={t("compositions.imageAlt", { id: part.content_id })} loading="lazy" />);
}

function QueueImages({ item }) {
  const { t } = useI18n();
  return (item.media?.items || []).filter(media => media.media_id && media.mime_detected?.startsWith("image/")).map(media => <img key={media.media_id} className="composition-image" src={`/api/v1/media/${item.content_id}/${media.media_id}`} alt={t("compositions.imageAlt", { id: item.content_id })} loading="lazy" />);
}

export function CompositionsPage({ api, run, parseId }) {
  const { t, lang } = useI18n();
  const [term, setTerm] = useState("");
  const [result, setResult] = useState(null);
  const [queue, setQueue] = useState([]);
  const [name, setName] = useState(""), [description, setDescription] = useState(""), [tags, setTags] = useState("");
  const [created, setCreated] = useState(null), [savedDetail, setSavedDetail] = useState(null), [warning, setWarning] = useState(null);
  const [filter, setFilter] = useState(""), [memberTags, setMemberTags] = useState(false), [existing, setExisting] = useState(null);
  const [lookupName, setLookupName] = useState(""), [lookupId, setLookupId] = useState("");
  const [directoryError, setDirectoryError] = useState(null);
  const serial = useRef(0), dragged = useRef(null), active = useRef(true), submittedTerm = useRef("");
  const directoryRequest = useRef(0);
  const submittedDirectory = useRef({ name: "", id: "", tags: [], memberTags: false });

  useEffect(() => {
    active.current = true;
    api("search?sort=time&limit=40&page=1").then(data => { if (active.current) setResult(data); }).catch(error => { if (active.current) setWarning({ error }); });
    const request = ++directoryRequest.current;
    api("compositions?limit=50&offset=0").then(data => { if (active.current && directoryRequest.current === request) setExisting(data); }).catch(error => { if (active.current && directoryRequest.current === request) setDirectoryError(error); });
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
      if (!resolved) throw localizedError("compositions.detailError");
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
    if (!name.trim() || !queue.length) throw localizedError("compositions.needName");
    const saved = await api("compositions", { name, description, content_ids: queue.map(item => item.content_id) });
    setCreated(saved.composition_id);
    if (names(tags).length) {
      try { await api(`compositions/${saved.composition_id}/tags/add`, { names: names(tags), namespace: "general" }); }
      catch (error) { setWarning({ tagFailureId: saved.composition_id, error }); await refreshDirectory({ propagate: false }); return; }
    }
    setWarning(null);
    await refreshDirectory({ propagate: false });
    setSavedDetail(await api(`compositions/${saved.composition_id}`));
  }
  async function refreshDirectory({ propagate = true } = {}) {
    const request = ++directoryRequest.current;
    try {
      const data = await api("compositions?limit=50&offset=0");
      if (active.current && directoryRequest.current === request) {
        setExisting(data); setDirectoryError(null);
        submittedDirectory.current = { name: "", id: "", tags: [], memberTags: false };
        setLookupName(""); setLookupId(""); setFilter(""); setMemberTags(false);
      }
    } catch (error) {
      if (active.current && directoryRequest.current === request) {
        setExisting(null); setDirectoryError(error);
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
        setExisting(data); setDirectoryError(null);
      }
    } catch (error) {
      if (active.current && directoryRequest.current === request) {
        setExisting(null); setDirectoryError(error);
      }
      throw error;
    }
  }
  async function retryTags() {
    if (!created || !names(tags).length) return;
    await api(`compositions/${created}/tags/add`, { names: names(tags), namespace: "general" });
    setWarning(null);
    await refreshDirectory({ propagate: false });
    setSavedDetail(await api(`compositions/${created}`));
  }
  const warningText = !warning ? "" : warning.tagFailureId
    ? t("compositions.tagFailure", { id: warning.tagFailureId, reason: describeError(warning.error, t) })
    : describeError(warning.error, t);

  return <div className="page compositions-page">
    <PageHeader eyebrow={t("compositions.eyebrow")} title={t("compositions.title")} description={t("compositions.description")} actions={<a className="button button-primary" href="#create-composition"><Icon name="plus" size={16} />{t("compositions.create")}</a>} />

    <section className="surface directory-panel" aria-label={t("compositions.directoryAria")}>
      <div className="section-heading">
        <div><p className="eyebrow">{t("compositions.directoryEyebrow")}</p><h3>{t("compositions.directoryTitle")}</h3></div>
        <p>{t("compositions.directoryIntro")}</p>
      </div>
      <form className="directory-form" onSubmit={event => { event.preventDefault(); run(() => browse()); }}>
        <label className="field"><span className="field-label">{t("compositions.idLabel")}</span><input type="number" min="1" step="1" value={lookupId} onChange={event => setLookupId(event.target.value)} placeholder={t("compositions.idPlaceholder")} /></label>
        <label className="field"><span className="field-label">{t("compositions.nameLabel")}</span><input value={lookupName} maxLength="255" onChange={event => setLookupName(event.target.value)} placeholder={t("compositions.namePlaceholder")} /></label>
        <label className="field"><span className="field-label">{t("compositions.filterLabel")}</span><textarea rows="1" value={filter} onChange={event => setFilter(event.target.value)} placeholder={t("compositions.filterPlaceholder")} /></label>
        <label className="checkbox-row directory-check"><input type="checkbox" checked={memberTags} onChange={event => setMemberTags(event.target.checked)} />{t("compositions.memberTags")}</label>
        <button type="submit" className="button button-ghost"><Icon name="search" size={15} />{t("compositions.find")}</button>
      </form>
      {directoryError && <p className="inline-notice tone-danger" role="alert"><Icon name="warning" size={16} /><span>{describeError(directoryError, t)}</span><button type="button" className="text-button" onClick={() => run(refreshDirectory)}>{t("compositions.retryDirectory")}</button></p>}
      {existing ? <div className="directory-body">
        <div className="directory-actions"><p>{t("compositions.total", { total: existing.total, count: existing.total })}</p><a className="quiet-link" href="#create-composition">{t("compositions.jumpCreate")} <Icon name="arrowDown" size={14} /></a></div>
        {existing.total === 0 && <EmptyState icon="layers" title={t("compositions.emptyTitle")}><p>{t("compositions.emptyBody")}</p></EmptyState>}
        <ul className="composition-results" tabIndex="0" aria-label={t("compositions.resultsAria")}>{existing.compositions.map(item => <li key={item.composition_id}>
          <div className="composition-card">
            <div className="composition-card-head">
              <div><span className="composition-serial">COMPOSITION {String(item.composition_id).padStart(2, "0")}</span><strong>{t("compositions.cardName", { name: item.name, id: item.composition_id })}</strong></div>
              <div className="composition-card-actions"><Link className="button button-small button-ghost" to={`/compositions/${item.composition_id}`}>{t("compositions.manage", { id: item.composition_id })}</Link><Link className="quiet-link" to={`/model?composition_id=${item.composition_id}`}>{t("compositions.testModel")}</Link></div>
            </div>
            <div className="composition-own-tags"><span>{t("compositions.ownTags")}</span><TagList tags={item.tags || []} empty={<span className="muted">{t("compositions.noOwnTags")}</span>} /><small>{t("compositions.noPropagate")}</small></div>
            <ol className="composition-mini-sequence">{item.members.map(member => <li key={member.position}>
              <span className="mini-position">{String(member.position + 1).padStart(2, "0")}</span>
              <span>{member.available ? <><TypeMark type={member.item_type} compact /> <span className="mini-text">{compactText(member.text || member.original_name, 64) || t("compositions.itemFallback", { id: member.content_id })}</span></> : t("compositions.unavailablePosition")}</span>
            </li>)}</ol>
          </div>
        </li>)}</ul>
        <div className="result-footer"><button className="button button-ghost" disabled={!existing.has_more} onClick={() => run(() => browse(existing.offset + existing.limit))}>{t("compositions.nextPage")} <Icon name="arrowRight" size={15} /></button><span>{existing.has_more ? t("compositions.more") : t("compositions.shown", { count: existing.compositions.length })}</span></div>
      </div> : !directoryError && <div className="loading-lines" aria-label={t("compositions.loadingAria")}><span /><span /><span /></div>}
    </section>

    <section className="surface builder-panel" id="create-composition">
      <div className="section-heading">
        <div><p className="eyebrow">{t("compositions.builderEyebrow")}</p><h3>{t("compositions.builderTitle")}</h3></div>
        <p>{t("compositions.builderIntro")}</p>
      </div>
      <div className="composition-layout">
        <section className="builder-candidates" aria-label={t("compositions.candidatesAria")}>
          <div className="builder-section-head"><span className="step-badge">01</span><div><h3>{t("compositions.stepSearch")}</h3><p>{t("compositions.stepSearchHint")}</p></div></div>
          <form className="candidate-search" onSubmit={event => { event.preventDefault(); run(() => search()); }}>
            <label className="search-input"><Icon name="search" size={17} /><span className="sr-only">{t("compositions.keywordAria")}</span><input aria-label={t("compositions.keywordAria")} value={term} onChange={event => setTerm(event.target.value)} maxLength="4096" placeholder={t("compositions.keywordPlaceholder")} /></label>
            <button type="submit" className="button button-ghost">{t("compositions.searchItems")}</button>
          </form>
          <p className="candidate-summary">{result ? t("compositions.summary", { page: result.page, count: result.hits.length }) : t("compositions.loadingItems")}</p>
          <div className="candidate-list">
            {(result?.hits || []).map(hit => <article className="candidate-message" key={hit.message_id}>
              <div className="candidate-message-head"><span>{t("compositions.message", { id: hit.message_id })}</span><span>{t("compositions.visibleItems", { count: hit.parts?.length || 0 })}</span></div>
              {(hit.parts || []).map(part => <div className="composition-candidate" key={part.content_id}>
                <label className="candidate-check"><input type="checkbox" aria-label={t("compositions.addItem", { id: part.content_id })} checked={queue.some(item => item.content_id === part.content_id)} onChange={event => { if (event.target.checked) run(() => add(part)); else setQueue(items => items.filter(item => item.content_id !== part.content_id)); }} /><span className="check-visual"><Icon name="check" size={12} /></span><span><TypeMark type={part.item_type} compact /> {t("compositions.itemLabel", { id: part.content_id })}</span></label>
                {part.text && <p className="composition-preview">{part.text}</p>}
                <CandidateImages part={part} />
              </div>)}
            </article>)}
          </div>
          <div className="result-footer"><button type="button" className="button button-ghost" disabled={!result?.has_more} onClick={() => run(() => search(true))}>{t("compositions.nextItems")} <Icon name="arrowRight" size={15} /></button></div>
        </section>

        <section className="builder-queue" aria-label={t("compositions.queueAria")}>
          <div className="builder-section-head"><span className="step-badge">02</span><div><h3>{t("compositions.queueTitle")}</h3><p>{t("compositions.queueHint")}</p></div><span className="queue-count">{t("compositions.queueCount", { count: queue.length })}</span></div>
          {!queue.length && <EmptyState icon="layers" title={t("compositions.queueEmptyTitle")}><p>{t("compositions.queueEmptyBody")}</p></EmptyState>}
          <ol className="composition-queue">{queue.map((item, index) => <li key={item.instance} draggable onDragStart={() => { dragged.current = index; }} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (dragged.current !== null) move(dragged.current, index); dragged.current = null; }} onDragEnd={() => { dragged.current = null; }}>
            <span className="queue-handle"><Icon name="drag" size={16} /></span>
            <span className="queue-index">{String(index + 1).padStart(2, "0")}</span>
            <div className="queue-copy"><div className="queue-meta"><TypeMark type={item.item_type} compact /><span>{t("compositions.itemLabel", { id: item.content_id })}</span></div>{item.text && <p className="composition-preview">{item.text}</p>}<QueueImages item={item} /></div>
            <div className="queue-actions"><button type="button" className="icon-button" disabled={index === 0} aria-label={t("compositions.moveUp", { position: index + 1, id: item.content_id })} onClick={() => move(index, index - 1)}><Icon name="arrowUp" size={15} /></button><button type="button" className="icon-button" disabled={index === queue.length - 1} aria-label={t("compositions.moveDown", { position: index + 1, id: item.content_id })} onClick={() => move(index, index + 1)}><Icon name="arrowDown" size={15} /></button><button type="button" className="icon-button" aria-label={t("compositions.reAdd", { position: index + 1, id: item.content_id })} onClick={() => run(() => add(item))}><Icon name="plus" size={15} /></button><button type="button" className="icon-button is-danger" aria-label={t("compositions.removePosition", { position: index + 1, id: item.content_id })} onClick={() => setQueue(items => items.filter(entry => entry.instance !== item.instance))}><Icon name="close" size={15} /></button></div>
          </li>)}</ol>

          <div className="composition-meta-form">
            <label className="field"><span className="field-label">{t("compositions.nameField")}</span><input value={name} maxLength="255" onChange={event => setName(event.target.value)} placeholder={t("compositions.nameFieldPlaceholder")} /></label>
            <label className="field"><span className="field-label">{t("compositions.descriptionField")}</span><textarea value={description} onChange={event => setDescription(event.target.value)} placeholder={t("compositions.descriptionPlaceholder")} /></label>
            <label className="field"><span className="field-label">{t("compositions.tagsField")}</span><textarea aria-label={t("compositions.tagsField")} value={tags} onChange={event => setTags(event.target.value)} placeholder={t("compositions.tagsPlaceholder")} /><span className="field-help">{t("compositions.tagsHelp")}</span></label>
            <button type="button" className="button button-primary button-wide" disabled={!name.trim() || !queue.length || Boolean(created)} onClick={() => run(create)}><Icon name="layers" size={16} />{t("compositions.submit")}</button>
          </div>
          {created && <div className="created-receipt"><span className="receipt-mark"><Icon name="check" size={18} /></span><div><strong>{t("compositions.createdTitle", { id: created })}</strong><p>{t("compositions.createdBody")}</p><div className="action-row"><Link className="text-link" to={`/compositions/${created}`}>{t("compositions.manage", { id: created })} <Icon name="arrowRight" size={14} /></Link><Link className="text-link" to={`/model?composition_id=${created}`}>{t("compositions.testModel")} <Icon name="arrowRight" size={14} /></Link></div></div></div>}
          {savedDetail && <p className="saved-sequence">{t("compositions.savedSequence", {
            sequence: savedDetail.members.map(member => member.available ? t("compositions.savedItem", { id: member.content_id }) : t("compositions.savedMissing", { position: member.position })).join(" → "),
            tags: savedDetail.tags.map(tag => tag.name).join(t("compositions.tagSeparator")) || t("compositions.savedTagsEmpty"),
          })}</p>}
          {warning && <p role="alert" className="inline-notice tone-danger"><Icon name="warning" size={16} /><span>{warningText}</span>{created && names(tags).length > 0 && <button className="text-button" onClick={() => run(retryTags)}>{t("compositions.retryTags")}</button>}</p>}
        </section>
      </div>
    </section>
  </div>;
}
