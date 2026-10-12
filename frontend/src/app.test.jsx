import React from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { Shell, safeId } from "./app.jsx";

const body = '<script>window.bad=true</script>中文正文';
const hit = { message_id: 3, item_type: "text", text: body, tags: [], source: "manual", first_seen_at: "2026-10-04" };
const detail = { message_id: 3, source: "manual", origin_note: "备注", parts: [{ content_id: 7, item_type: "text", text: body, media: [] }] };
let calls, uploadHandler;
beforeEach(() => {
  calls = []; uploadHandler = null; window.history.replaceState(null, "", "/");
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/uploads") && uploadHandler) return uploadHandler(url, options);
    let data = {};
    if (url.endsWith("/session")) data = { csrf: "test-csrf" };
    else if (url.includes("/search?")) data = { hits: [hit], page: 1, total: 1, total_kind: "exact", has_more: false };
    else if (url.endsWith("/model/status") || url.endsWith("/model/key/set") || url.endsWith("/model/access")) data = { key_present: true, armed: true, enabled: true };
    else if (url.endsWith("/model/preview")) data = { preview_id: "preview-one", provider: "deepseek", model: "deepseek-flash", endpoint: "https://api.deepseek.com/chat/completions", parts: [{ position: 0, item_type: "text", text: body, images: [] }], candidate_catalog: [{ candidate_id: 1, name: "已有", namespace: "general" }], candidate_count: 1, expires_in_seconds: 300, attempts_remaining: 20, metadata_warning: "原始图片含 EXIF", unknown_outcome_warning: "结果未知可能计费", catalog_warning: "完整候选目录", request_bytes: 42 };
    else if (url.endsWith("/model/send")) data = { status: "succeeded", request_id: "preview-one" };
    else if (url.endsWith("/tags")) data = { tags: [] };
    else if (url.includes("/detail?")) data = detail;
    else if (url.includes("/assignments?")) data = { assignments: [{ assign_id: 9, content_id: 7, name: "建议", source: "suggested", confidence: 0.8, revoked_at: null, evidence: { model_proposal: { run_id: 12, version: "b4d9551a-7876-4933-b52f-98b8bacde1e8" } } }] };
    else if (url.includes("/aliases?")) data = { aliases: [{ alias: "别名", name: "测试", namespace: "general" }] };
    else if (url.endsWith("/uploads")) data = { message_id: 3, duplicate: false, status: "succeeded", content_ids: [7], detail: "内容处理完成" };
    return { ok: true, status: 200, json: async () => data };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
async function open(path = "/") {
  render(<MemoryRouter initialEntries={[path]}><Shell /></MemoryRouter>);
  await screen.findByText(/本机临时 root；/);
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe("操作完成"));
  return userEvent.setup();
}

test("导航保留新增草稿与管理表单", async () => {
  const user = await open("/add");
  await user.type(screen.getByLabelText("文本"), "草稿中文");
  await user.type(screen.getByLabelText("备注"), "待提交备注");
  const file = new File(["image"], "same.png", { type: "image/png" });
  await user.upload(screen.getByLabelText("图片"), file);
  await user.click(screen.getByRole("link", { name: "检索 / 管理已有内容" }));
  await user.type(screen.getByLabelText("关键词"), "保留检索条件");
  await user.click(screen.getByRole("link", { name: "加入新内容" }));
  expect(screen.getByLabelText("文本").value).toBe("草稿中文");
  expect(screen.getByLabelText("备注").value).toBe("待提交备注");
  expect(screen.getByText("same.png (5 字节)")).toBeTruthy();
  await user.click(screen.getByRole("link", { name: "检索 / 管理已有内容" }));
  expect(screen.getByLabelText("关键词").value).toBe("保留检索条件");
});

test("上传使用 multipart 与 CSRF，成功清空草稿并打开详情", async () => {
  const user = await open("/add");
  await user.type(screen.getByLabelText("文本"), "新正文");
  await user.upload(screen.getByLabelText("图片"), new File(["png"], "a.png", { type: "image/png" }));
  await user.click(screen.getByRole("button", { name: "加入内容" }));
  const call = calls.find(x => x.url.endsWith("/uploads"));
  expect(call.options.body).toBeInstanceOf(FormData);
  expect(call.options.body.get("text")).toBe("新正文");
  expect(call.options.headers["X-CSRF-Token"]).toBe("test-csrf");
  expect(call.options.headers["Content-Type"]).toBeUndefined();
  await screen.findByRole("link", { name: "查看本次消息" });
  expect(screen.getByLabelText("文本").value).toBe("");
  await user.click(screen.getByRole("link", { name: "查看本次消息" }));
  expect(await screen.findByText(body, { selector: "pre" })).toBeTruthy();
  expect(document.querySelector("script")).toBeNull();
  expect(window.bad).toBeUndefined();
});

test("提交中禁止重复操作，失败保留草稿", async () => {
  const user = await open("/add");
  let finish;
  uploadHandler = () => new Promise(resolve => { finish = resolve; });
  await user.type(screen.getByLabelText("文本"), "保留失败草稿");
  await user.click(screen.getByRole("button", { name: "加入内容" }));
  expect(screen.getByRole("button", { name: "加入内容" }).closest("fieldset").disabled).toBe(true);
  await user.click(screen.getByRole("button", { name: "加入内容" }));
  expect(calls.filter(x => x.url.endsWith("/uploads"))).toHaveLength(1);
  finish({ ok: false, status: 413, json: async () => ({ detail: "大小超过限额" }) });
  await screen.findByText("大小超过限额");
  expect(screen.getByLabelText("文本").value).toBe("保留失败草稿");
});

test("隔离结果不给隐藏内容详情入口", async () => {
  const user = await open("/add");
  uploadHandler = async () => ({ ok: true, status: 200, json: async () => ({ message_id: 5, duplicate: false, status: "quarantined", content_ids: [], detail: "已隔离" }) });
  await user.type(screen.getByLabelText("文本"), "隔离样本");
  await user.click(screen.getByRole("button", { name: "加入内容" }));
  await screen.findByText(/消息 5：已隔离/);
  expect(screen.queryByRole("link", { name: "查看本次消息" })).toBeNull();
});

test("管理页继续支持打标、建议、别名与系统动作", async () => {
  const user = await open("/manage");
  await user.click(screen.getByLabelText("选择消息 3"));
  await user.type(screen.getByLabelText("标签名称（每行一个）"), "测试");
  await user.click(screen.getByRole("button", { name: "添加标签" }));
  await waitFor(() => expect(calls.some(x => x.url.endsWith("/tags/add"))).toBe(true));
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe("操作完成"));
  await user.click(screen.getByLabelText("选择消息 3"));
  await user.click(screen.getByRole("button", { name: "查看目标关联记录" }));
  await user.click(await screen.findByLabelText("选择建议 9"));
  await user.click(screen.getByRole("button", { name: "确认勾选建议" }));
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe("操作完成"));
  await user.type(screen.getByLabelText("规范标签名"), "测试");
  await user.type(screen.getByLabelText("别名"), "别名");
  await user.click(screen.getByRole("button", { name: "添加别名" }));
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe("操作完成"));
  const hitView = screen.getAllByRole("button", { name: "查看", exact: true })[0];
  await user.click(hitView);
  await screen.findByText(body, { selector: "pre" });
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe("操作完成"));
  await user.click(screen.getByRole("button", { name: "默认程序打开" }));
  await waitFor(() => expect(calls.some(x => x.url.endsWith("/actions/open"))).toBe(true));
  expect(JSON.parse(calls.find(x => x.url.endsWith("/suggestions/confirm")).options.body)).toEqual({ ids: [9], expected_runs: { 9: { run_id: 12, version: "b4d9551a-7876-4933-b52f-98b8bacde1e8" } } });
  expect(calls.some(x => x.url.endsWith("/aliases/add"))).toBe(true);
});

test("授权交换清除 fragment，保留路径和查询参数", async () => {
  window.history.replaceState(null, "", "/manage?message_id=3#token=one-use");
  await open("/manage?message_id=3");
  expect(window.location.pathname).toBe("/manage");
  expect(window.location.search).toBe("?message_id=3");
  expect(window.location.hash).toBe("");
  expect(JSON.parse(calls.find(x => x.url.endsWith("/session")).options.body)).toEqual({ token: "one-use" });
});

test("模型密钥提交即清空，总闸要求明确勾选", async () => {
  const user = await open("/model");
  await user.type(screen.getByLabelText("模型 API Key"), "TEST-ONLY-BROWSER-KEY");
  await user.click(screen.getByRole("button", { name: "设置内存密钥" }));
  await waitFor(() => expect(screen.getByLabelText("模型 API Key").value).toBe(""));
  expect(JSON.parse(calls.find(x => x.url.endsWith("/model/key/set")).options.body)).toEqual({ key: "TEST-ONLY-BROWSER-KEY" });
  expect(screen.getByRole("button", { name: "开启模型外发" }).disabled).toBe(true);
  await user.click(screen.getByLabelText("我确认开启模型外发总闸"));
  await user.click(screen.getByRole("button", { name: "开启模型外发" }));
  await waitFor(() => expect(calls.some(x => x.url.endsWith("/model/access"))).toBe(true));
  expect(JSON.parse(calls.find(x => x.url.endsWith("/model/access")).options.body)).toEqual({ enabled: true, confirmed: true });
});

test("模型预览展示正文及完整目录，逐次同意后只发送一次", async () => {
  const user = await open("/model");
  await user.type(screen.getByLabelText("模型目标 ID"), "7");
  await user.click(screen.getByRole("button", { name: "生成外发预览" }));
  expect(await screen.findByText(body, { selector: "pre" })).toBeTruthy();
  expect(screen.getByText("已有 (general)")).toBeTruthy();
  expect(document.querySelector("script")).toBeNull();
  expect(calls.some(x => x.url.endsWith("/model/send"))).toBe(false);
  expect(screen.getByRole("button", { name: "发送本次预览" }).disabled).toBe(true);
  await user.click(screen.getByLabelText("我同意将本次预览发送给模型"));
  await user.click(screen.getByRole("button", { name: "发送本次预览" }));
  await screen.findByText(/模型结果：succeeded/);
  expect(calls.filter(x => x.url.endsWith("/model/send"))).toHaveLength(1);
  expect(JSON.parse(calls.find(x => x.url.endsWith("/model/send")).options.body)).toEqual({ preview_id: "preview-one", confirmed: true });
  await user.click(screen.getByRole("button", { name: "查看模型目标建议" }));
  await user.click(await screen.findByLabelText("模型选择建议 9"));
  await user.click(screen.getByRole("button", { name: "确认模型建议" }));
  await waitFor(() => expect(calls.some(x => x.url.endsWith("/suggestions/confirm"))).toBe(true));
  expect(JSON.parse(calls.find(x => x.url.endsWith("/suggestions/confirm")).options.body).expected_runs[9].run_id).toBe(12);
});

test("关闭模型总闸提交明确关闭并清空页面授权", async () => {
  const user = await open("/model");
  await user.type(screen.getByLabelText("模型目标 ID"), "7");
  await user.click(screen.getByRole("button", { name: "生成外发预览" }));
  await screen.findByText("本次外发预览");
  await user.click(screen.getByRole("button", { name: "关闭模型外发并清除密钥" }));
  await waitFor(() => expect(calls.some(x => x.url.endsWith("/model/access"))).toBe(true));
  expect(JSON.parse(calls.find(x => x.url.endsWith("/model/access")).options.body)).toEqual({ enabled: false, confirmed: true });
  expect(screen.queryByText("本次外发预览")).toBeNull();
});

test("拒绝浏览器无法精确表示的 ID", () => {
  expect(() => safeId("9007199254740993")).toThrow();
  expect(safeId("42")).toBe(42);
});
