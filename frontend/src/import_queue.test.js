import { expect, test } from "vitest";
import { assignQueuedMessage, changeQueuedImage, buildUploadForm, compositionMembers, expectedPartCount, groupQueuedMessage, messages, queueMessage, readyForComposition } from "./import_queue.js";

const draft = (text, images = []) => ({ text, note: "备注", images, entries: images.map(file => ({ kind: "image", file })), orderTouched: false });

test("暂存消息保持独立文件与单一组合归属", () => {
  const file = new File(["image"], "a.png", { type: "image/png" });
  const first = queueMessage([], draft("第一条", [file]));
  const second = queueMessage(first, draft("第二条"));
  const grouped = groupQueuedMessage(second, second[1].id, "组合", "");
  const group = grouped.find(item => item.kind === "group");
  const moved = assignQueuedMessage(grouped, first[0].id, group.id);
  expect(moved).toHaveLength(1);
  expect(messages(moved).map(item => item.draft.text)).toEqual(["第二条", "第一条"]);
  expect(messages(moved)[1].draft.images[0]).toBe(file);
  expect(first[0].kind).toBe("message");
  const split = changeQueuedImage(moved, first[0].id, 0, true);
  expect(split).toHaveLength(1);
  expect(split[0].id).toBe(group.id);
  expect(split[0].members.map(item => item.draft.text)).toEqual(["第二条", "第一条", ""]);
  const separated = split[0].members[2];
  expect(separated.id).not.toBe(first[0].id);
  expect(buildUploadForm(separated.draft).get("images")).toBe(file);
  expect(separated.draft.note).toBe("备注");
  expect(buildUploadForm(split[0].members[1].draft).getAll("images")).toEqual([]);
  expect(changeQueuedImage(split, separated.id, 0, true)).toBe(split);
  expect(changeQueuedImage(split, separated.id, 0)[0].members).toHaveLength(2);
  const standalone = changeQueuedImage(first, first[0].id, 0, true);
  expect(standalone.map(item => item.kind)).toEqual(["message", "message"]);
  expect(standalone[0].id).toBe(first[0].id);
  expect(changeQueuedImage(first, first[0].id, 0)[0].draft.images).toEqual([]);
  const single = queueMessage([], draft("", [file]));
  const singleGroup = groupQueuedMessage(single, single[0].id, "单图组", "");
  expect(changeQueuedImage(singleGroup, single[0].id, 0)).toEqual([]);
  const locked = [{ ...first[0], status: "succeeded" }];
  expect(changeQueuedImage(locked, first[0].id, 0, true)).toBe(locked);
});

test("有序图片上传体不改变旧上传字段", () => {
  const file = new File(["pic"], "a.png", { type: "image/png" });
  const form = buildUploadForm({ ...draft("开头", [file]), orderTouched: true, entries: [{ kind: "link", url: "https://example.org", description: "网页" }, { kind: "image", file }] });
  expect(JSON.parse(form.get("parts"))).toEqual([{ kind: "text", text: "开头" }, { kind: "link", url: "https://example.org", description: "网页" }, { kind: "image", index: 0 }]);
  expect(form.get("text")).toBeNull();
  expect(form.get("images")).toBe(file);
  expect(form.get("note")).toBe("备注");
});

test("可在归档前计算组合成员上限", () => {
  expect(expectedPartCount(draft("开头", [new File(["a"], "a.png")]))).toBe(2);
  expect(expectedPartCount({ ...draft(""), entries: [{ kind: "link", url: "https://example.org", description: "" }, { kind: "text", text: "后段" }] })).toBe(2);
});

test("组合成员保留队列顺序及重复引用，未完成消息不能创建组合", () => {
  const items = queueMessage(queueMessage([], draft("重复")), draft("重复"));
  const grouped = groupQueuedMessage(items, items[0].id, "组合", "");
  const id = grouped[0].id;
  const queued = assignQueuedMessage(grouped, items[1].id, id);
  expect(readyForComposition(queued, id)).toBe(false);
  const saved = [{ ...queued[0], members: queued[0].members.map((item, index) => ({ ...item, status: "succeeded", result: { message_id: 7, content_ids: index ? [11] : [11, 12] } })) }];
  expect(readyForComposition(saved, id)).toBe(true);
  expect(compositionMembers(saved, id)).toEqual([11, 12, 11]);
  expect(readyForComposition(saved, "empty")).toBe(false);
  for (const patch of [{ result: { content_ids: [] } }, { status: "unresolved" }, { result: { content_ids: Array(501).fill(1) } }]) {
    expect(readyForComposition([{ ...saved[0], members: [{ ...saved[0].members[0], ...patch }] }], id)).toBe(false);
  }
});
