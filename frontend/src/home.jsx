import React, { useEffect, useState } from "react";
import { Link } from "react-router";
import { describeError, useI18n } from "./i18n/index.jsx";
import { compactText, EmptyState, formatDate, Icon, PageHeader, TagList, TypeMark } from "./ui.jsx";

export function HomePage({ api }) {
  const { t, lang } = useI18n();
  const [recent, setRecent] = useState(null);
  const [collections, setCollections] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let active = true;
    Promise.all([
      api("search?sort=time&limit=6&page=1&result_version=2"),
      api("compositions?limit=5&offset=0"),
    ]).then(([search, shelf]) => {
      if (!active) return;
      setRecent(search);
      setCollections(shelf);
    }).catch(caught => { if (active) setError(caught); });
    return () => { active = false; };
  }, [api]);

  const hits = recent?.hits || [];
  const compositions = collections?.compositions || [];
  const latest = hits[0];

  return <div className="page home-page">
    <PageHeader
      eyebrow={t("home.eyebrow")}
      title={t("home.title")}
      description={t("home.description")}
      actions={<><Link className="button button-primary" to="/add"><Icon name="plus" size={16} />{t("home.collect")}</Link><Link className="button button-ghost" to="/manage"><Icon name="search" size={16} />{t("home.enterArchive")}</Link></>}
    />

    {error && <div className="inline-notice tone-danger" role="alert"><Icon name="warning" size={16} /><span>{describeError(error, t)}</span></div>}

    <section className="home-hero surface" aria-label={t("home.overviewAria")}>
      <div className="hero-copy">
        <span className="hero-index">01</span>
        <p className="eyebrow">{t("home.heroEyebrow")}</p>
        <h2>{t("home.heroTitle1")}<br />{t("home.heroTitle2")}</h2>
        <p className="hero-note">{t("home.heroNote")}</p>
        <div className="hero-actions">
          <Link to="/manage" className="text-link">{t("home.browseRecent")} <Icon name="arrowRight" size={15} /></Link>
          <Link to="/compositions" className="text-link">{t("home.openShelf")} <Icon name="arrowRight" size={15} /></Link>
        </div>
      </div>
      <div className="hero-stats">
        <div className="hero-stat">
          <span className="stat-number">{recent ? hits.length : "—"}</span>
          <span className="stat-label">{t("home.statRecent")}</span>
        </div>
        <div className="hero-stat">
          <span className="stat-number">{collections ? collections.total : "—"}</span>
          <span className="stat-label">{t("home.statCompositions")}</span>
        </div>
        <div className="hero-quote">
          <Icon name="bookOpen" size={19} />
          <p>{latest ? compactText(latest.text || latest.name || latest.original_name, 62) : t("home.emptyQuote")}</p>
          {latest && <span>{latest.kind === "composition" ? t("home.latestComposition") : t("home.latestArchive")}</span>}
        </div>
      </div>
    </section>

    <div className="home-grid">
      <section className="surface home-panel recent-panel">
        <div className="section-heading">
          <div><p className="eyebrow">{t("home.recentEyebrow")}</p><h2>{t("home.recentTitle")}</h2></div>
          <Link className="quiet-link" to="/manage">{t("home.viewAll")} <Icon name="arrowRight" size={14} /></Link>
        </div>
        {!recent && <div className="loading-lines" aria-label={t("home.loadingRecentAria")}><span /><span /><span /></div>}
        {recent && !hits.length && <EmptyState icon="archive" title={t("home.emptyTitle")} action={<Link className="button button-primary" to="/add">{t("home.firstPage")}</Link>}>{t("home.emptyBody")}</EmptyState>}
        <div className="recent-list">
          {hits.map((hit, index) => <Link className="recent-item" key={`${hit.kind}:${hit.entity_id}`} to={hit.kind === "composition" ? `/manage?composition_id=${hit.composition_id}` : `/manage?content_id=${hit.content_id}`}>
            <span className="recent-order">{String(index + 1).padStart(2, "0")}</span>
            <span className="recent-main">
              <span className="recent-meta"><TypeMark kind={hit.kind} type={hit.item_type} compact /><span>{formatDate(hit.sort_time, false, lang)}</span></span>
              <strong>{compactText(hit.kind === "composition" ? hit.name : (hit.text || hit.original_name), 96) || (hit.kind === "composition" ? t("home.untitledComposition") : t("home.noText"))}</strong>
              <TagList tags={hit.tags || []} limit={3} />
            </span>
            <Icon name="arrowRight" size={16} className="recent-arrow" />
          </Link>)}
        </div>
      </section>

      <aside className="surface home-panel shelf-panel">
        <div className="section-heading">
          <div><p className="eyebrow">{t("home.shelfEyebrow")}</p><h2>{t("home.shelfTitle")}</h2></div>
          <Link className="quiet-link" to="/compositions">{t("home.organize")} <Icon name="arrowRight" size={14} /></Link>
        </div>
        <p className="section-intro">{t("home.shelfIntro")}</p>
        {!collections && <div className="loading-lines"><span /><span /></div>}
        {collections && !compositions.length && <EmptyState icon="layers" title={t("home.noCompositions")} action={<Link className="button button-primary" to="/compositions#create-composition">{t("home.createComposition")}</Link>}>{t("home.shelfEmptyBody")}</EmptyState>}
        <ol className="shelf-list">
          {compositions.map((item, index) => <li key={item.composition_id}>
            <Link to={`/compositions/${item.composition_id}`} className="shelf-item">
              <span className="shelf-number">{String(index + 1).padStart(2, "0")}</span>
              <span className="shelf-copy"><strong>{item.name}</strong><small>{t("home.shelfMeta", { count: item.members.length, tags: item.tags.map(tag => `#${tag.name}`).join(" ") || t("home.noOwnTags") })}</small></span>
              <Icon name="chevronRight" size={15} />
            </Link>
          </li>)}
        </ol>
        <div className="shelf-note"><Icon name="layers" size={16} /><span>{t("home.shelfNote")}</span></div>
      </aside>
    </div>

    <section className="workflow-strip" aria-label={t("home.workflowAria")}>
      <div className="workflow-title"><span className="eyebrow">{t("home.workflowEyebrow")}</span><h2>{t("home.workflowTitle")}</h2></div>
      <div className="workflow-step"><span>01</span><strong>{t("home.step1Title")}</strong><p>{t("home.step1Body")}</p></div>
      <div className="workflow-step"><span>02</span><strong>{t("home.step2Title")}</strong><p>{t("home.step2Body")}</p></div>
      <div className="workflow-step"><span>03</span><strong>{t("home.step3Title")}</strong><p>{t("home.step3Body")}</p></div>
    </section>
  </div>;
}
