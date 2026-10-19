import React from "react";

const paths = {
  home: <><path d="M4 10.5 12 4l8 6.5"/><path d="M6.5 9.5V20h11V9.5"/><path d="M10 20v-5h4v5"/></>,
  plus: <><path d="M12 5v14M5 12h14"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 4.5 4.5"/></>,
  library: <><path d="M5 4.5h4v15H5zM11 4.5h4v15h-4zM17 5.5l2.8-.7 3 14.7-2.8.7z"/></>,
  layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 16 9 5 9-5"/></>,
  sparkles: <><path d="m12 3 1.4 4.1L17.5 8.5l-4.1 1.4L12 14l-1.4-4.1-4.1-1.4 4.1-1.4L12 3Z"/><path d="m19 14 .8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14Z"/><path d="m5 14 .6 1.4L7 16l-1.4.6L5 18l-.6-1.4L3 16l1.4-.6L5 14Z"/></>,
  logout: <><path d="M10 5H5v14h5"/><path d="M14 8l4 4-4 4M8 12h10"/></>,
  arrowRight: <><path d="M5 12h14M14 7l5 5-5 5"/></>,
  arrowUp: <><path d="M12 19V5M7 10l5-5 5 5"/></>,
  arrowDown: <><path d="M12 5v14M7 14l5 5 5-5"/></>,
  chevronLeft: <path d="m15 5-7 7 7 7"/>,
  chevronRight: <path d="m9 5 7 7-7 7"/>,
  close: <><path d="m6 6 12 12M18 6 6 18"/></>,
  image: <><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="9.5" r="1.5"/><path d="m5 17 4.5-4.5 3 3 2.5-2.5 4 4"/></>,
  link: <><path d="M10.5 13.5a4 4 0 0 0 5.7 0l2.3-2.3a4 4 0 1 0-5.7-5.7L11.6 6.7"/><path d="M13.5 10.5a4 4 0 0 0-5.7 0l-2.3 2.3a4 4 0 1 0 5.7 5.7l1.2-1.2"/></>,
  text: <><path d="M5 6V4.5h14V6M12 4.5v15M8.5 19.5h7"/></>,
  tag: <><path d="M4 4h7l9 9-7 7-9-9V4Z"/><circle cx="8.5" cy="8.5" r="1.2"/></>,
  drag: <><circle cx="9" cy="7" r=".8" fill="currentColor"/><circle cx="15" cy="7" r=".8" fill="currentColor"/><circle cx="9" cy="12" r=".8" fill="currentColor"/><circle cx="15" cy="12" r=".8" fill="currentColor"/><circle cx="9" cy="17" r=".8" fill="currentColor"/><circle cx="15" cy="17" r=".8" fill="currentColor"/></>,
  eye: <><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/></>,
  external: <><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v6H5V6h6"/></>,
  check: <path d="m5 12 4.5 4.5L19 7"/>,
  archive: <><rect x="3.5" y="4.5" width="17" height="5" rx="1"/><path d="M5 9.5V20h14V9.5M9.5 13h5"/></>,
  clock: <><circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/></>,
  hash: <><path d="M9 4 7 20M17 4l-2 16M4 9h16M3 15h16"/></>,
  filter: <><path d="M4 6h16M7 12h10M10 18h4"/></>,
  trash: <><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5M14 11v5"/></>,
  refresh: <><path d="M19 8a8 8 0 1 0 1 6"/><path d="M19 4v4h-4"/></>,
  bookOpen: <><path d="M3.5 5.5c3-1 6 .2 8.5 2v12c-2.5-1.8-5.5-3-8.5-2v-12Z"/><path d="M20.5 5.5c-3-1-6 .2-8.5 2v12c2.5-1.8 5.5-3 8.5-2v-12Z"/></>,
  pin: <><path d="m9 4 6 6M8 9l7-7M6 11l7 7M13.5 9.5l-4 4-4.5 1 1-4.5 4-4"/><path d="m4 20 5-5"/></>,
  more: <><circle cx="5" cy="12" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><circle cx="19" cy="12" r="1.2" fill="currentColor"/></>,
  warning: <><path d="M12 3.5 21 20H3L12 3.5Z"/><path d="M12 9v5M12 17.2v.2"/></>,
  key: <><circle cx="8" cy="15" r="3.5"/><path d="m10.5 12.5 8-8M16 7l2 2M13 10l2 2"/></>,
  copy: <><rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></>,
  shield: <><path d="M12 3 20 6v5c0 5-3.4 8.5-8 10-4.6-1.5-8-5-8-10V6l8-3Z"/><path d="m8.5 12 2.2 2.2 4.8-5"/></>,
};

export function Icon({ name, size = 18, className = "" }) {
  return <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.more}</svg>;
}

export const typeLabels = {
  text: "文字", image: "图片", mixed: "图文", link: "链接", video: "视频",
  audio: "音频", sticker: "贴纸", file: "文件", composition: "组合", message: "消息",
};

export function TypeMark({ type, kind = "", compact = false }) {
  const label = kind === "composition" || type === "composition" ? "组合" : (typeLabels[type] || "内容");
  const icon = kind === "composition" || type === "composition" ? "layers" : type === "image" ? "image" : type === "link" ? "link" : "text";
  return <span className={`type-mark type-${type || kind || "unknown"}${compact ? " is-compact" : ""}`}><Icon name={icon} size={compact ? 12 : 14} />{label}</span>;
}

export function TagPill({ tag, onRemove, removeLabel, tone = "neutral" }) {
  const name = typeof tag === "string" ? tag : tag?.name;
  const namespace = typeof tag === "string" ? "" : tag?.namespace;
  if (!name) return null;
  return <span className={`tag-pill tone-${tone}`} title={namespace ? `命名空间：${namespace}` : undefined}>
    <span className="tag-hash">#</span>{name}
    {onRemove && <button type="button" className="tag-remove" aria-label={removeLabel || `撤销标签 ${name}`} onClick={onRemove}><Icon name="close" size={11} /></button>}
  </span>;
}

export function TagList({ tags = [], limit = 8, empty = null, onRemove, removeLabel }) {
  const items = tags.filter(Boolean);
  if (!items.length) return empty;
  return <div className="tag-list">{items.slice(0, limit).map((tag, index) => <TagPill key={`${tag?.namespace || ""}:${tag?.name || tag}:${index}`} tag={tag} onRemove={onRemove ? () => onRemove(tag) : undefined} removeLabel={typeof removeLabel === "function" ? removeLabel(tag) : removeLabel} />)}{items.length > limit && <span className="tag-more">+{items.length - limit}</span>}</div>;
}

const dateFormatter = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

export function formatDate(value, withTime = false) {
  if (!value) return "时间未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return (withTime ? dateTimeFormatter : dateFormatter).format(date);
}

export function compactText(value, length = 180) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

export function statusLabel(status) {
  return ({ succeeded: "已完成", queued: "等待处理", running: "处理中", failed: "处理失败", quarantined: "已隔离", dead: "任务终止" })[status] || status || "未知状态";
}

export function PageHeader({ eyebrow, title, description, actions, meta }) {
  return <header className="page-header">
    <div className="page-heading">
      {eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h1>{title}</h1>
      {description && <p className="page-description">{description}</p>}
      {meta && <div className="page-meta">{meta}</div>}
    </div>
    {actions && <div className="page-actions">{actions}</div>}
  </header>;
}

export function EmptyState({ icon = "archive", title, children, action }) {
  return <div className="empty-state">
    <span className="empty-icon"><Icon name={icon} size={24} /></span>
    <h3>{title}</h3>
    {children && <div className="empty-state-copy">{children}</div>}
    {action}
  </div>;
}

export function InlineNotice({ tone = "info", children }) {
  if (!children) return null;
  return <div className={`inline-notice tone-${tone}`} role={tone === "danger" ? "alert" : undefined}>
    <Icon name={tone === "danger" ? "warning" : "check"} size={16} />
    <span>{children}</span>
  </div>;
}
