import React from "react";
import { readFileSync } from "node:fs";
const webCss = readFileSync("src/style.css", "utf8");
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { Shell, safeId } from "./app.jsx";
import { safeWebUrl } from "./links.jsx";

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
    else if (url.includes("/compositions?")) data = { result_version: 1, compositions: [], total: 0, offset: 0, limit: 50, has_more: false };
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

test("组合目录进入即列出持久化组合且创建后刷新", async () => {
  const original = globalThis.fetch;
  let created = false;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/compositions?") && options?.method === "GET") {
      calls.push({ url, options });
      return { ok: true, status: 200, json: async () => ({ result_version: 1, total: created ? 2 : 1, limit: 50, offset: 0, has_more: false, compositions: [
        { composition_id: 4, name: "已有组合", tags: [], members: [{ position: 0, content_id: 7, available: true, item_type: "text", text: "旧正文", media: [] }] },
        ...(created ? [{ composition_id: 12, name: "新组合", tags: [], members: [{ position: 0, content_id: 7, available: true, item_type: "text", text: "新正文", media: [] }] }] : []),
      ] }) };
    }
    if (url.includes("/search?")) return { ok: true, status: 200, json: async () => ({ hits: [{ ...hit, parts: [{ content_id: 7, item_type: "text", text: "新正文", media: { items: [] } }] }], page: 1, has_more: false }) };
    if (url.endsWith("/compositions") && options?.method === "POST") { created = true; return { ok: true, status: 200, json: async () => ({ composition_id: 12 }) }; }
    if (url.endsWith("/compositions/12")) return { ok: true, status: 200, json: async () => ({ members: [{ position: 0, content_id: 7, available: true }], tags: [] }) };
    return original(url, options);
  }));
  const user = await open("/compositions");
  expect(await screen.findByText(/已有组合（组合 4）/)).toBeTruthy();
  await user.click(await screen.findByLabelText("加入内容项 7"));
  await user.type(screen.getByLabelText("组合名称"), "新组合");
  await user.click(screen.getByRole("button", { name: "确认构建组合" }));
  expect(await screen.findByText(/新组合（组合 12）/)).toBeTruthy();
  expect(calls.filter(x => x.url.includes("/compositions?")).length).toBeGreaterThanOrEqual(2);
});

test("组合目录提供创建区快捷入口和有界结果容器", async () => {
  const user = await open("/compositions");
  const shortcut = screen.getByRole("link", { name: "跳到创建新组合" });
  expect(shortcut.getAttribute("href")).toBe("#create-composition");
  expect(document.getElementById("create-composition")).toBeTruthy();
  expect(screen.getByRole("region", { name: "已有组合目录" }).querySelector(".composition-results")).toBeTruthy();
  await user.click(shortcut);
});

test("组合目录延迟的首次加载不会覆盖用户提交的筛选", async () => {
  const original = globalThis.fetch;
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/compositions?")) {
      if (url.includes("name=")) return { ok: true, status: 200, json: async () => ({ compositions: [{ composition_id: 9, name: "筛选结果", tags: [], members: [] }], total: 1, offset: 0, limit: 50, has_more: false }) };
      return pending;
    }
    return original(url, options);
  }));
  const user = await open("/compositions");
  await user.type(screen.getByLabelText("组合名称包含"), "筛选");
  await user.click(screen.getByRole("button", { name: "查找组合" }));
  expect(await screen.findByText(/筛选结果（组合 9）/)).toBeTruthy();
  await React.act(async () => { release({ ok: true, status: 200, json: async () => ({ compositions: [{ composition_id: 4, name: "旧结果", tags: [], members: [] }], total: 1, offset: 0, limit: 50, has_more: false }) }); await pending; });
  expect(screen.queryByText(/旧结果（组合 4）/)).toBeNull();
  expect(screen.getByText(/筛选结果（组合 9）/)).toBeTruthy();
});

test("组合目录首次加载失败可在页面重试", async () => {
  const original = globalThis.fetch;
  let attempts = 0;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/compositions?")) {
      attempts++;
      return attempts === 1 ? { ok: false, status: 503, json: async () => ({ detail: "暂时不可用" }) } : { ok: true, status: 200, json: async () => ({ compositions: [], total: 0, offset: 0, limit: 50, has_more: false }) };
    }
    return original(url, options);
  }));
  const user = await open("/compositions");
  expect(await screen.findByText(/暂时不可用/)).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "重试读取组合" }));
  expect(await screen.findByText("共 0 个组合")).toBeTruthy();
  expect(attempts).toBe(2);
});

test("创建后详情读取失败仍刷新已有组合目录", async () => {
  const original = globalThis.fetch;
  let created = false;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/search?")) return { ok: true, status: 200, json: async () => ({ hits: [{ ...hit, parts: [{ content_id: 7, item_type: "text", text: body, media: { items: [] } }] }], page: 1, has_more: false }) };
    if (url.includes("/compositions?")) return { ok: true, status: 200, json: async () => ({ compositions: created ? [{ composition_id: 12, name: "已保存", tags: [], members: [{ position: 0, content_id: 7, available: true, item_type: "text" }] }] : [], total: created ? 1 : 0, offset: 0, limit: 50, has_more: false }) };
    if (url.endsWith("/compositions") && options?.method === "POST") { created = true; return { ok: true, status: 200, json: async () => ({ composition_id: 12 }) }; }
    if (url.endsWith("/compositions/12")) return { ok: false, status: 503, json: async () => ({ detail: "详情暂不可用" }) };
    return original(url, options);
  }));
  const user = await open("/compositions");
  await user.click(await screen.findByLabelText("加入内容项 7"));
  await user.type(screen.getByLabelText("组合名称"), "已保存");
  await user.click(screen.getByRole("button", { name: "确认构建组合" }));
  expect(await screen.findByText(/已保存（组合 12）/)).toBeTruthy();
});

test("组合创建成功后即使目录刷新失败也显示保存顺序", async () => {
  const original = globalThis.fetch;
  let created = false;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/search?")) return { ok: true, status: 200, json: async () => ({ hits: [{ ...hit, parts: [{ content_id: 7, item_type: "text", text: body, media: { items: [] } }] }], page: 1, has_more: false }) };
    if (url.includes("/compositions?")) return created ? { ok: false, status: 503, json: async () => ({ detail: "目录暂不可用" }) } : { ok: true, status: 200, json: async () => ({ compositions: [], total: 0, offset: 0, limit: 50, has_more: false }) };
    if (url.endsWith("/compositions") && options?.method === "POST") { created = true; return { ok: true, status: 200, json: async () => ({ composition_id: 12 }) }; }
    if (url.endsWith("/compositions/12")) return { ok: true, status: 200, json: async () => ({ members: [{ position: 0, content_id: 7, available: true }], tags: [] }) };
    return original(url, options);
  }));
  const user = await open("/compositions");
  await user.click(await screen.findByLabelText("加入内容项 7"));
  await user.type(screen.getByLabelText("组合名称"), "仍可核对");
  await user.click(screen.getByRole("button", { name: "确认构建组合" }));
  expect(await screen.findByText(/保存顺序：内容项 7/)).toBeTruthy();
  expect(await screen.findByText(/目录暂不可用/)).toBeTruthy();
});

test("目录查找失败不显示旧的组合结果", async () => {
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/compositions?")) return url.includes("name=") ? { ok: false, status: 400, json: async () => ({ detail: "标签不存在" }) } : { ok: true, status: 200, json: async () => ({ compositions: [{ composition_id: 4, name: "旧列表", tags: [], members: [] }], total: 1, limit: 50, offset: 0, has_more: false }) };
    return original(url, options);
  }));
  const user = await open("/compositions");
  await screen.findByText(/旧列表（组合 4）/);
  await user.type(screen.getByLabelText("组合名称包含"), "没有");
  await user.click(screen.getByRole("button", { name: "查找组合" }));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("标签不存在"));
  expect(screen.queryByText(/旧列表（组合 4）/)).toBeNull();
});

test("过大组合 ID 的错误显示在目录旁", async () => {
  const user = await open("/compositions");
  await user.type(screen.getByLabelText("组合 ID"), "9007199254740992");
  await user.click(screen.getByRole("button", { name: "查找组合" }));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("ID 必须"));
});

test("组合页从混合帖选择具体部件并依次构建组合", async () => {
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/search?")) return { ok: true, status: 200, json: async () => ({ hits: [{ ...hit, parts: [{ content_id: 7, item_type: "text", text: "文案A", media: { items: [] } }, { content_id: 8, item_type: "image", text: "", media: { items: [{ media_id: 4, mime_detected: "image/png" }] } }] }], page: 1, total: 1, total_kind: "exact", has_more: false }) };
    if (url.endsWith("/compositions") && options?.method === "POST") { calls.push({ url, options }); return { ok: true, status: 200, json: async () => ({ composition_id: 12 }) }; }
    if (url.endsWith("/compositions/12")) return { ok: true, status: 200, json: async () => ({ composition_id: 12, name: "测试组合", members: [{ position: 0, content_id: 8, available: true, item_type: "image", media: [], text: "" }, { position: 1, content_id: 7, available: true, item_type: "text", media: [], text: "文案A" }], tags: [{ name: "图文" }] }) };
    if (url.endsWith("/compositions/12/tags/add")) { calls.push({ url, options }); return { ok: true, status: 200, json: async () => ({ changed_tag_ids: [1] }) }; }
    return original(url, options);
  }));
  const user = await open("/compositions");
  await user.click(await screen.findByLabelText("加入内容项 7"));
  await user.click(screen.getByLabelText("加入内容项 8"));
  await user.click(screen.getByRole("button", { name: "上移位置 2 的内容项 8" }));
  await user.type(screen.getByLabelText("组合名称"), "测试组合");
  await user.type(screen.getByLabelText("组合标签（每行一个）"), "图文");
  await user.click(screen.getByRole("button", { name: "确认构建组合" }));
  await screen.findByText(/组合 12 已创建/);
  expect(await screen.findByText(/保存顺序：内容项 8 → 内容项 7/)).toBeTruthy();
  const created = calls.find(x => x.url.endsWith("/compositions") && x.options.method === "POST");
  expect(JSON.parse(created.options.body)).toEqual({ name: "测试组合", description: "", content_ids: [8, 7] });
  expect(JSON.parse(calls.find(x => x.url.endsWith("/compositions/12/tags/add")).options.body)).toEqual({ names: ["图文"], namespace: "general" });
  await user.click(screen.getByRole("link", { name: "用此组合测试模型建议" }));
  expect(screen.getByLabelText("模型目标类型").value).toBe("composition_id");
  expect(screen.getByLabelText("模型目标 ID").value).toBe("12");
});

test("检索结果没有媒体 ID 时通过详情加载图片预览", async () => {
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/search?")) return { ok: true, status: 200, json: async () => ({ hits: [{ ...hit, parts: [{ content_id: 8, item_type: "image", text: "", media: { count: 1, items: [{ mime_detected: "image/png" }] } }] }], page: 1, has_more: false }) };
    if (url.includes("/detail?content_id=8")) return { ok: true, status: 200, json: async () => ({ parts: [{ content_id: 8, item_type: "image", text: "", media: [{ media_id: 41, mime_detected: "image/png" }] }] }) };
    return original(url, options);
  }));
  const user = await open("/compositions");
  await user.click(await screen.findByLabelText("加入内容项 8"));
  await waitFor(() => expect(screen.getByRole("region", { name: "组合顺序和确认区" }).querySelector("img")?.getAttribute("src")).toBe("/api/v1/media/8/41"));
  expect(document.querySelector('img[src$="undefined"]')).toBeNull();
});

test("组合队列跨检索保留、重复加入并支持拖放和移除", async () => {
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/search?")) return { ok: true, status: 200, json: async () => ({ hits: [{ ...hit, parts: [{ content_id: 7, item_type: "text", text: "文案A", media: { items: [] } }, { content_id: 8, item_type: "image", text: "", media: { items: [] } }] }], page: 1, has_more: false }) };
    if (url.endsWith("/compositions") && options?.method === "POST") { calls.push({ url, options }); return { ok: true, status: 200, json: async () => ({ composition_id: 12 }) }; }
    if (url.endsWith("/compositions/12")) return { ok: true, status: 200, json: async () => ({ members: [], tags: [] }) };
    return original(url, options);
  }));
  const user = await open("/compositions");
  await user.click(await screen.findByLabelText("加入内容项 7"));
  await user.click(screen.getByLabelText("加入内容项 8"));
  await user.type(screen.getByLabelText("内容项关键词"), "其他条件");
  await user.click(screen.getByRole("button", { name: "检索内容项" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "再次加入位置 1 的内容项 7" })).toBeTruthy());
  await user.click(screen.getByRole("button", { name: "再次加入位置 1 的内容项 7" }));
  const rows = document.querySelectorAll(".composition-queue li");
  const { fireEvent } = await import("@testing-library/react");
  fireEvent.dragStart(rows[1]); fireEvent.dragOver(rows[0]); fireEvent.drop(rows[0]);
  expect(document.querySelector(".composition-queue li").textContent).toContain("内容项 8");
  await user.click(screen.getByRole("button", { name: "移除位置 3 的内容项 7" }));
  await user.type(screen.getByLabelText("组合名称"), "顺序");
  await user.click(screen.getByRole("button", { name: "确认构建组合" }));
  await screen.findByText(/组合 12 已创建/);
  expect(JSON.parse(calls.find(x => x.url.endsWith("/compositions") && x.options.method === "POST").options.body).content_ids).toEqual([8, 7]);
});

test("翻页沿用已提交的组合检索关键词", async () => {
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/search?")) { calls.push({ url, options }); return { ok: true, status: 200, json: async () => ({ hits: [], page: url.includes("page=2") ? 2 : 1, next_cursor: "cursor-one", has_more: true }) }; }
    return original(url, options);
  }));
  const user = await open("/compositions");
  await user.type(screen.getByLabelText("内容项关键词"), "初始词");
  await user.click(screen.getByRole("button", { name: "检索内容项" }));
  await user.type(screen.getByLabelText("内容项关键词"), "未提交");
  await user.click(screen.getByRole("button", { name: "下一页内容" }));
  await waitFor(() => expect(calls.some(x => x.url.includes("page=2"))).toBe(true));
  const next = new URL(calls.find(x => x.url.includes("page=2")).url, "http://localhost");
  expect(next.searchParams.get("q")).toBe("初始词");
});

test("组合页按自身标签检索既有组合", async () => {
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/compositions?")) { calls.push({ url, options }); return { ok: true, status: 200, json: async () => ({ compositions: [{ composition_id: 12, name: "已建组合", tags: [{ name: "图文" }], members: [{ position: 0, content_id: 7, available: true, item_type: "text", text: "文案", media: [] }] }], has_more: false, total: 1 }) }; }
    return original(url, options);
  }));
  const user = await open("/compositions");
  await user.type(screen.getByLabelText("检索组合标签（每行一个）"), "图文");
  await user.click(screen.getByRole("button", { name: "查找组合" }));
  expect(await screen.findByText(/已建组合/)).toBeTruthy();
  await waitFor(() => expect(calls.filter(x => x.url.includes("/compositions?")).at(-1).url).toContain("tags=%E5%9B%BE%E6%96%87"));
});

test("组合目录按名称和 ID 查找并保留翻页条件", async () => {
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/compositions?")) { calls.push({ url, options }); return { ok: true, status: 200, json: async () => ({ compositions: [{ composition_id: 12, name: "午后手记", tags: [], members: [{ position: 0, content_id: 7, available: true, item_type: "text", text: "正文", media: [] }] }], has_more: true, offset: url.includes("offset=50") ? 50 : 0, limit: 50, total: 51 }) }; }
    return original(url, options);
  }));
  const user = await open("/compositions");
  await screen.findByText(/午后手记/);
  await user.type(screen.getByLabelText("组合名称包含"), "午后");
  await user.type(screen.getByLabelText("组合 ID"), "12");
  await user.click(screen.getByRole("button", { name: "查找组合" }));
  await waitFor(() => expect(calls.some(x => x.url.includes("name=%E5%8D%88%E5%90%8E"))).toBe(true));
  await user.type(screen.getByLabelText("组合名称包含"), "未提交");
  await user.click(screen.getByRole("button", { name: "下一页组合" }));
  const next = new URL(calls.find(x => x.url.includes("offset=50")).url, "http://localhost");
  expect(next.searchParams.get("name")).toBe("午后");
  expect(next.searchParams.get("composition_id")).toBe("12");
});

test("统一检索同页展示内容项和组合并可筛选查看顺序", async () => {
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/search?") && url.includes("result_version=2")) {
      calls.push({ url, options });
      return { ok: true, status: 200, json: async () => ({ result_version: 2, hits: [
        { kind: "composition", entity_id: 12, composition_id: 12, name: "新组合", description: "两段内容", item_type: "composition", sort_time: "2026-10-02", tags: [] },
        { kind: "content", entity_id: 7, content_id: 7, message_id: 3, item_type: "text", text: "正文", sort_time: "2026-10-01", tags: [] },
      ], page: 1, has_more: false, total: null, total_kind: "not_counted" }) };
    }
    if (url.endsWith("/compositions/12")) return { ok: true, status: 200, json: async () => ({
      composition_id: 12, name: "新组合", description: "两段内容", tags: [], members: [
        { position: 0, content_id: 7, item_type: "text", text: "先读", available: true, media: [] },
        { position: 1, content_id: 8, item_type: "text", text: "后读", available: true, media: [] },
      ],
    }) };
    return original(url, options);
  }));
  const user = await open("/manage");
  expect(await screen.findByText("新组合")).toBeTruthy();
  expect(screen.getByText("正文")).toBeTruthy();
  expect(screen.getByRole("columnheader", { name: "结果 ID" })).toBeTruthy();
  expect(screen.queryByRole("option", { name: "结果中勾选的消息" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "查看组合 12" }));
  await screen.findByText("先读");
  expect(Array.from(document.querySelectorAll("#parts article")).map(item => item.textContent)).toEqual(["内容项 7 (text) 先读", "内容项 8 (text) 后读"]);
  await user.selectOptions(screen.getByLabelText("结果类型"), "composition");
  await user.click(screen.getByRole("button", { name: "检索 / 返回第一页" }));
  const search = calls.filter(call => call.url.includes("/search?")).at(-1).url;
  expect(new URL(search, "http://localhost").searchParams.get("kind")).toBe("composition");
});


test.each(["翻页", "重新检索"])("统一检索%s后标签操作不包含旧选择", async mode => {
  const original = globalThis.fetch;
  let searches = 0;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/search?")) {
      const id = ++searches === 1 ? 7 : 8;
      return { ok: true, status: 200, json: async () => ({ result_version: 2,
        hits: [{ kind: "content", entity_id: id, content_id: id, item_type: "text", text: `正文 ${id}`, tags: [] }],
        page: searches, has_more: true, next_cursor: "next", total: null, total_kind: "not_counted",
      }) };
    }
    return original(url, options);
  }));
  const user = await open("/manage");
  await user.click(screen.getByLabelText("选择内容项 7"));
  if (mode === "重新检索") await user.type(screen.getByLabelText("关键词"), "新关键词");
  await user.click(screen.getByRole("button", { name: mode === "翻页" ? "下一页" : "检索 / 返回第一页" }));
  await screen.findByLabelText("选择内容项 8");
  await user.click(screen.getByRole("button", { name: "添加标签" }));
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe("请勾选标签操作目标"));
  expect(calls.some(call => call.url.endsWith("/tags/add"))).toBe(false);
  await user.click(screen.getByLabelText("选择内容项 8"));
  await user.type(screen.getByLabelText("标签名称（每行一个）"), "新标签");
  await user.click(screen.getByRole("button", { name: "添加标签" }));
  await waitFor(() => expect(calls.some(call => call.url.endsWith("/tags/add"))).toBe(true));
  expect(JSON.parse(calls.find(call => call.url.endsWith("/tags/add")).options.body).content_ids).toEqual([8]);
});

test("统一检索解释混合组合并提供整理入口", async () => {
  const user = await open("/manage");
  expect(screen.getByText("混合组合（成员含多种类型）")).toBeTruthy();
  await user.click(screen.getByRole("link", { name: "整理已有组合" }));
  expect(await screen.findByRole("heading", { name: "已有组合" })).toBeTruthy();
});

test("网页解除固定宽度上限", () => {
  const style = document.createElement("style");
  style.textContent = webCss;
  document.head.append(style);
  try {
    expect(style.sheet?.cssRules.length).toBeGreaterThan(0);
    expect(getComputedStyle(document.body).fontFamily).toBe("sans-serif");
    expect(["", "none"]).toContain(getComputedStyle(document.body).maxWidth);
  } finally {
    style.remove();
  }
});

test("管理页样式不再定义窄屏单列覆盖", () => {
  const style = document.createElement("style");
  style.textContent = webCss;
  document.head.append(style);
  try {
    const layoutRules = Array.from(style.sheet.cssRules).flatMap(rule => [rule, ...Array.from(rule.cssRules || [])]);
    const columns = layoutRules.filter(rule => rule.selectorText === ".manage-layout")
      .map(rule => rule.style.getPropertyValue("grid-template-columns")).filter(Boolean);
    expect(columns).toEqual(["minmax(0,2fr) minmax(0,1fr)"]);
  } finally {
    style.remove();
  }
});

test("管理页全宽双栏提供可访问的局部横向滚动", async () => {
  await open("/manage");
  const viewport = screen.getByRole("region", { name: "检索与详情" });
  const layout = viewport.querySelector(".manage-layout");
  const search = screen.getByRole("heading", { name: "检索内容项与组合" }).closest("section");
  const detailPanel = screen.getByRole("heading", { name: "详情与本机操作" }).closest("section");
  const tags = screen.getByRole("heading", { name: "人工标签与关联" }).closest("section");
  expect(viewport.tabIndex).toBe(0);
  expect(Array.from(layout.children)).toEqual([search, detailPanel]);
  expect(viewport.contains(tags)).toBe(false);
  expect(search.querySelector(".table-wrap #hits")).toBeTruthy();
});

test("管理页双栏让内容标签检索与完整详情并列且标签管理仍在下方", async () => {
  const longText = "很长的正文".repeat(180);
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/detail?")) return { ok: true, status: 200, json: async () => ({ ...detail, parts: [{ content_id: 7, item_type: "image", text: longText, media: [{ media_id: 42, mime_detected: "image/png" }] }] }) };
    return original(url, options);
  }));
  const user = await open("/manage");
  const layout = document.querySelector(".manage-layout");
  const search = screen.getByRole("heading", { name: "检索内容项与组合" }).closest("section");
  const detailPanel = screen.getByRole("heading", { name: "详情与本机操作" }).closest("section");
  const tags = screen.getByRole("heading", { name: "人工标签与关联" }).closest("section");
  const catalog = screen.getByRole("heading", { name: "标签目录与别名" }).closest("section");
  expect(layout).not.toBeNull();
  expect(Array.from(layout.children)).toEqual([search, detailPanel]);
  expect(search.contains(screen.getByLabelText("标签（每行一个，全部匹配）"))).toBe(true);
  expect(search.querySelector(".table-wrap #hits")).toBeTruthy();
  expect(layout.contains(tags) || layout.contains(catalog)).toBe(false);
  await user.click(search.querySelector("#hits button"));
  await waitFor(() => expect(detailPanel.querySelector("#parts pre")?.textContent).toBe(longText));
  expect(detailPanel.querySelector("#parts img")?.getAttribute("src")).toBe("/api/v1/media/7/42");
  expect(detailPanel.contains(screen.getByRole("button", { name: "默认程序打开" }))).toBe(true);
});

test("多行诗歌的检索摘要受限但详情保留全文", async () => {
  const poem = "长段".repeat(120) + "\n" + Array.from({ length: 18 }, (_, index) => `诗句${index}`).join("\n");
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/search?")) return { ok: true, status: 200, json: async () => ({ hits: [{ ...hit, snippet: poem }], page: 1, total: 1, total_kind: "exact", has_more: false }) };
    if (url.includes("/detail?")) return { ok: true, status: 200, json: async () => ({ ...detail, parts: [{ ...detail.parts[0], text: poem }] }) };
    return original(url, options);
  }));
  const user = await open("/manage");
  const preview = document.querySelector("#hits .summary .summary-clamp");
  expect(preview).not.toBeNull();
  expect(preview.textContent.length).toBeLessThanOrEqual(180);
  await user.click(screen.getAllByRole("button", { name: "查看", exact: true })[0]);
  await waitFor(() => expect(document.querySelector("#parts pre")?.textContent).toBe(poem));
});

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

test("URL 中格式控制字符不得成为可点击目标", () => {
  expect(safeWebUrl("https://example.org/\u202eevil")).toBeNull();
});


test("正文 URL 可点击而脚本协议保持纯文本", async () => {
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url, options) => {
    if (url.includes("/detail?")) return { ok: true, status: 200, json: async () => ({ ...detail, parts: [{ content_id: 7, item_type: "text", text: "看(https://example.org/a)，javascript:alert(1)", media: [] }] }) };
    return original(url, options);
  }));
  const user = await open("/manage");
  await user.click(screen.getAllByRole("button", { name: "查看", exact: true })[0]);
  const link = await screen.findByRole("link", { name: "https://example.org/a" });
  expect(link.getAttribute("href")).toBe("https://example.org/a");
  expect(screen.queryByRole("link", { name: /javascript:/ })).toBeNull();
});


test("有序编辑器按调整后的顺序提交文本图片和链接", async () => {
  const user = await open("/add");
  await user.click(screen.getByRole("button", { name: "添加文本条目" }));
  await user.type(screen.getByLabelText("条目 1 文本"), "文本 A");
  await user.upload(screen.getByLabelText("图片"), new File(["png"], "b.png", { type: "image/png" }));
  await user.click(screen.getByRole("button", { name: "添加链接条目" }));
  await user.type(screen.getByLabelText("条目 3 URL"), "https://example.org");
  await user.type(screen.getByLabelText("条目 3 说明"), "人工说明");
  await user.click(screen.getByRole("button", { name: "上移条目 3" }));
  await user.click(screen.getByRole("button", { name: "加入内容" }));
  const sent = calls.find(call => call.url.endsWith("/uploads")).options.body;
  expect(JSON.parse(sent.get("parts"))).toEqual([
    { kind: "text", text: "文本 A" },
    { kind: "link", url: "https://example.org", description: "人工说明" },
    { kind: "image", index: 0 },
  ]);
});


test("仅图片队列重排后按显示顺序上传", async () => {
  const user = await open("/add");
  await user.upload(screen.getByLabelText("图片"), [
    new File(["a"], "first.png", { type: "image/png" }),
    new File(["b"], "second.png", { type: "image/png" }),
  ]);
  await user.click(screen.getByRole("button", { name: "下移条目 1" }));
  await user.click(screen.getByRole("button", { name: "加入内容" }));
  const body = calls.find(call => call.url.endsWith("/uploads")).options.body;
  expect(body.getAll("images").map(file => file.name)).toEqual(["second.png", "first.png"]);
  expect(JSON.parse(body.get("parts"))).toEqual([{ kind: "image", index: 0 }, { kind: "image", index: 1 }]);
});


test("仅图片队列移除后只上传保留的图片", async () => {
  const user = await open("/add");
  await user.upload(screen.getByLabelText("图片"), [
    new File(["a"], "first.png", { type: "image/png" }),
    new File(["b"], "second.png", { type: "image/png" }),
  ]);
  await user.click(screen.getByRole("button", { name: "移除条目 1" }));
  await user.click(screen.getByRole("button", { name: "加入内容" }));
  const body = calls.find(call => call.url.endsWith("/uploads")).options.body;
  expect(body.getAll("images").map(file => file.name)).toEqual(["second.png"]);
  expect(JSON.parse(body.get("parts"))).toEqual([{ kind: "image", index: 0 }]);
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
