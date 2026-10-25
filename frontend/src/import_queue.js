// 待导入消息只保存在当前浏览器页面的内存中；File 不序列化。
export function buildUploadForm(draft, { atomic = false } = {}) {
  const orderMode = draft.orderTouched || draft.entries.some(entry => entry.kind !== "image");
  const images = orderMode ? draft.entries.filter(entry => entry.kind === "image").map(entry => entry.file) : draft.images;
  if (!draft.text.trim() && !images.length && !draft.entries.some(entry => entry.kind === "text" && entry.text.trim() || entry.kind === "link" && entry.url.trim())) throw new Error("请输入文本、链接或选择图片");
  if (images.length > 19) throw new Error("一次最多上传 19 张图片");
  const body = new FormData();
  if (orderMode) {
    const entries = draft.text.trim() ? [{ kind: "text", text: draft.text }, ...draft.entries] : draft.entries;
    let imageIndex = 0;
    body.append("parts", JSON.stringify(entries.map(entry => entry.kind === "image" ? { kind: "image", index: imageIndex++ } : entry)));
  } else body.append("text", draft.text);
  body.append("note", draft.note);
  images.forEach(file => body.append("images", file));
  if (atomic) {
    body.append("create_composition", "true"); body.append("composition_name", draft.compositionName);
    body.append("composition_description", draft.compositionDescription);
    body.append("composition_tags", draft.compositionTags);
  }
  return body;
}

export function expectedPartCount(draft) {
  const orderMode = draft.orderTouched || draft.entries.some(entry => entry.kind !== "image");
  return (draft.text.trim() ? 1 : 0) + (orderMode ? draft.entries.length : draft.images.length);
}

export function messages(queue) {
  return queue.flatMap(unit => unit.kind === "group" ? unit.members : [unit]);
}

export function queueMessage(queue, draft) {
  const snapshot = { ...draft, images: [...draft.images], entries: draft.entries.map(entry => ({ ...entry })) };
  return [...queue, { id: crypto.randomUUID(), kind: "message", draft: snapshot, status: "pending", result: null }];
}

export function editable(unit) {
  return unit.kind === "group" ? !unit.compositionId && !unit.uncertain && unit.members.every(item => item.status === "pending") : unit.status === "pending";
}

export function queuedImages(draft) {
  return draft.orderTouched || draft.entries.some(entry => entry.kind !== "image")
    ? draft.entries.filter(entry => entry.kind === "image").map(entry => entry.file) : draft.images;
}

// 按图片位置操作而非 File 身份，保留重复选择同一文件的其他位置。
export function changeQueuedImage(queue, id, position, split = false) {
  const item = messages(queue).find(entry => entry.id === id);
  const group = queue.find(unit => unit.members?.some(entry => entry.id === id));
  if (!item || !editable(item) || (group && !editable(group))) return queue;
  const images = queuedImages(item.draft);
  if (!Number.isInteger(position) || position < 0 || position >= images.length) return queue;
  const ordered = item.draft.orderTouched || item.draft.entries.some(entry => entry.kind !== "image");
  const entries = ordered ? item.draft.entries : images.map(file => ({ kind: "image", file }));
  let imagePosition = 0;
  const remaining = entries.filter(entry => entry.kind !== "image" || imagePosition++ !== position);
  const draft = { ...item.draft, entries: remaining, images: images.filter((_, index) => index !== position) };
  const hasContent = draft.text.trim() || remaining.some(entry => entry.kind === "image" || entry.kind === "text" && entry.text.trim() || entry.kind === "link" && entry.url.trim());
  // 纯单图已经是独立消息，无需再分离；删除最后内容则移除空消息。
  if (split && !hasContent) return queue;
  const replacements = hasContent ? [{ ...item, draft }] : [];
  if (split) replacements.push(...queueMessage([], {
    ...item.draft, text: "", images: [images[position]], entries: [{ kind: "image", file: images[position] }], orderTouched: false,
  }));
  const replace = items => items.flatMap(entry => entry.id === id ? replacements : [entry]);
  return (group ? queue.map(unit => unit.id === group.id ? { ...unit, members: replace(unit.members) } : unit) : replace(queue))
    .filter(unit => unit.kind !== "group" || unit.members.length);
}

// 拖放、键盘换序和归属调整共用此入口；beforeId 为空时追加。
export function moveQueuedUnit(queue, id, groupId = null, beforeId = null) {
  const unit = queue.find(item => item.id === id) || messages(queue).find(item => item.id === id);
  const source = queue.find(item => item.members?.some(member => member.id === id));
  const target = groupId ? queue.find(item => item.id === groupId && item.kind === "group") : null;
  if (!unit || !editable(unit) || (source && !editable(source)) || id === beforeId || id === groupId
    || (groupId && (!target || !editable(target) || unit.kind === "group"))) return queue;
  const next = queue.filter(item => item.id !== id).map(item => item.kind === "group" ? { ...item, members: item.members.filter(member => member.id !== id) } : item);
  const destination = groupId ? next.find(item => item.id === groupId).members : next;
  const index = beforeId ? destination.findIndex(item => item.id === beforeId) : destination.length;
  if (index < 0) return queue;
  destination.splice(index, 0, unit);
  return next.filter(item => item.kind !== "group" || item.members.length);
}

export function assignQueuedMessage(queue, id, groupId) {
  return moveQueuedUnit(queue, id, groupId);
}

export function groupQueuedMessage(queue, id, name, description) {
  const unit = messages(queue).find(item => item.id === id);
  const source = queue.find(item => item.members?.some(member => member.id === id));
  if (!unit || !editable(unit) || (source && !editable(source))) return queue;
  const next = moveQueuedUnit(queue, id).filter(item => item.id !== id);
  const position = queue.findIndex(item => item.id === id || item.id === source?.id);
  next.splice(position, 0, { id: crypto.randomUUID(), kind: "group", name, description, members: [unit], compositionId: null, uncertain: false });
  return next;
}

export function removeQueuedMessage(queue, id) {
  return queue.filter(item => item.id !== id).map(item => item.kind === "group" ? { ...item, members: item.members.filter(member => member.id !== id) } : item)
    .filter(item => item.kind !== "group" || item.members.length);
}

export function compositionMembers(queue, groupId) {
  return (queue.find(item => item.id === groupId)?.members || []).flatMap(item => item.result?.content_ids || []);
}

export function readyForComposition(queue, groupId) {
  const members = queue.find(item => item.id === groupId)?.members || [];
  return members.length > 0 && members.every(item => item.status === "succeeded" && item.result?.content_ids?.length > 0)
    && compositionMembers(queue, groupId).length <= 500;
}
