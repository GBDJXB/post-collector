import React, { useEffect, useState } from "react";
import { Link } from "react-router";
import { compactText, EmptyState, formatDate, Icon, PageHeader, TagList, TypeMark } from "./ui.jsx";

export function HomePage({ api }) {
  const [recent, setRecent] = useState(null);
  const [collections, setCollections] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    Promise.all([
      api("search?sort=time&limit=6&page=1&result_version=2"),
      api("compositions?limit=5&offset=0"),
    ]).then(([search, shelf]) => {
      if (!active) return;
      setRecent(search);
      setCollections(shelf);
    }).catch(caught => { if (active) setError(caught.message); });
    return () => { active = false; };
  }, [api]);

  const hits = recent?.hits || [];
  const compositions = collections?.compositions || [];
  const latest = hits[0];

  return <div className="page home-page">
    <PageHeader
      eyebrow="LOCAL READING ROOM / 本机阅读室"
      title="让收藏成为一条可以重走的路径"
      description="把散落在不同来源的文字、图片和链接收进来；用标签找回，用组合排出下一次阅读的顺序。"
      actions={<><Link className="button button-primary" to="/add"><Icon name="plus" size={16} />收集一页</Link><Link className="button button-ghost" to="/manage"><Icon name="search" size={16} />进入归档</Link></>}
    />

    {error && <div className="inline-notice tone-danger" role="alert"><Icon name="warning" size={16} /><span>{error}</span></div>}

    <section className="home-hero surface" aria-label="归档概览">
      <div className="hero-copy">
        <span className="hero-index">01</span>
        <p className="eyebrow">A PLACE FOR WHAT MATTERS</p>
        <h2>不是囤积，<br/>是把值得回看的东西留在手边。</h2>
        <p className="hero-note">归档保持内容原貌。标签帮你横向找，组合帮你纵向读；两者可以各自成立，也可以一起使用。</p>
        <div className="hero-actions">
          <Link to="/manage" className="text-link">浏览最近归档 <Icon name="arrowRight" size={15} /></Link>
          <Link to="/compositions" className="text-link">打开组合书架 <Icon name="arrowRight" size={15} /></Link>
        </div>
      </div>
      <div className="hero-stats">
        <div className="hero-stat">
          <span className="stat-number">{recent ? hits.length : "—"}</span>
          <span className="stat-label">最近载入的结果</span>
        </div>
        <div className="hero-stat">
          <span className="stat-number">{collections ? collections.total : "—"}</span>
          <span className="stat-label">个独立组合</span>
        </div>
        <div className="hero-quote">
          <Icon name="bookOpen" size={19} />
          <p>{latest ? compactText(latest.text || latest.name || latest.original_name, 62) : "下一次重新遇见，会从你此刻保存的内容开始。"}</p>
          {latest && <span>{latest.kind === "composition" ? "最近的组合" : "最近归档"}</span>}
        </div>
      </div>
    </section>

    <div className="home-grid">
      <section className="surface home-panel recent-panel">
        <div className="section-heading">
          <div><p className="eyebrow">RECENTLY SAVED</p><h2>最近归档</h2></div>
          <Link className="quiet-link" to="/manage">查看全部 <Icon name="arrowRight" size={14} /></Link>
        </div>
        {!recent && <div className="loading-lines" aria-label="正在读取最近归档"><span /><span /><span /></div>}
        {recent && !hits.length && <EmptyState icon="archive" title="归档还是空的" action={<Link className="button button-primary" to="/add">收集第一页</Link>}>从一段文字、一张图片或一个链接开始。</EmptyState>}
        <div className="recent-list">
          {hits.map((hit, index) => <Link className="recent-item" key={`${hit.kind}:${hit.entity_id}`} to={hit.kind === "composition" ? `/manage?composition_id=${hit.composition_id}` : `/manage?content_id=${hit.content_id}`}>
            <span className="recent-order">{String(index + 1).padStart(2, "0")}</span>
            <span className="recent-main">
              <span className="recent-meta"><TypeMark kind={hit.kind} type={hit.item_type} compact /><span>{formatDate(hit.sort_time)}</span></span>
              <strong>{compactText(hit.kind === "composition" ? hit.name : (hit.text || hit.original_name), 96) || (hit.kind === "composition" ? "未命名组合" : "无文字内容")}</strong>
              <TagList tags={hit.tags || []} limit={3} />
            </span>
            <Icon name="arrowRight" size={16} className="recent-arrow" />
          </Link>)}
        </div>
      </section>

      <aside className="surface home-panel shelf-panel">
        <div className="section-heading">
          <div><p className="eyebrow">READING PATHS</p><h2>组合书架</h2></div>
          <Link className="quiet-link" to="/compositions">整理 <Icon name="arrowRight" size={14} /></Link>
        </div>
        <p className="section-intro">组合是内容项的有序路径：成员可以来自不同消息，也可以重复出现。</p>
        {!collections && <div className="loading-lines"><span /><span /></div>}
        {collections && !compositions.length && <EmptyState icon="layers" title="还没有组合" action={<Link className="button button-primary" to="/compositions#create-composition">创建组合</Link>}>挑选几个内容项，为它们排一个阅读顺序。</EmptyState>}
        <ol className="shelf-list">
          {compositions.map((item, index) => <li key={item.composition_id}>
            <Link to={`/compositions/${item.composition_id}`} className="shelf-item">
              <span className="shelf-number">{String(index + 1).padStart(2, "0")}</span>
              <span className="shelf-copy"><strong>{item.name}</strong><small>{item.members.length} 个位置 · {item.tags.map(tag => `#${tag.name}`).join(" ") || "无自身标签"}</small></span>
              <Icon name="chevronRight" size={15} />
            </Link>
          </li>)}
        </ol>
        <div className="shelf-note"><Icon name="layers" size={16} /><span>组合标签只属于组合本身，不会复制给成员。</span></div>
      </aside>
    </div>

    <section className="workflow-strip" aria-label="使用方式">
      <div className="workflow-title"><span className="eyebrow">THREE MOVES</span><h2>收进来，找回来，读下去</h2></div>
      <div className="workflow-step"><span>01</span><strong>随手加入</strong><p>文字、图片与链接可以交错成一条有序消息。</p></div>
      <div className="workflow-step"><span>02</span><strong>检索与标注</strong><p>关键词、来源、类型和标签共同缩小范围。</p></div>
      <div className="workflow-step"><span>03</span><strong>编排组合</strong><p>把不同来源的内容项排成自己的阅读路线。</p></div>
    </section>
  </div>;
}
