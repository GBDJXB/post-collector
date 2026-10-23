import { expect, test } from "vitest";
import i18nSource from "./i18n/index.jsx?raw";
import { describeError, formatDate, formatToday, localizedError, messages, readStoredLang, translate } from "./i18n/index.jsx";

const sources = import.meta.glob("./*.jsx", { query: "?raw", import: "default", eager: true });
const tEn = (key, values) => translate("en", key, values);
const placeholders = text => [...String(text).matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort();

test("翻译取当前语言文案并替换占位符", () => {
  expect(translate("zh", "shell.progress")).toBe("正在处理……");
  expect(translate("en", "shell.progress")).not.toMatch(/[\u4e00-\u9fff]/);
  expect(translate("zh", "common.httpFailure", { status: 502 })).toContain("HTTP 502");
  expect(translate("en", "common.httpFailure", { status: 502 })).toContain("HTTP 502");
  expect(translate("en", "common.httpFailure", { status: 502 })).not.toMatch(/[\u4e00-\u9fff]/);
});

test("未知键回退为键名", () => {
  expect(translate("en", "no.such.key")).toBe("no.such.key");
  expect(translate("zh", "no.such.key")).toBe("no.such.key");
});

test("英文单数计数使用 .one 变体", () => {
  expect(translate("en", "add.partsCount", { count: 1 })).toBe("1 part");
  expect(translate("en", "add.partsCount", { count: 2 })).toBe("2 parts");
  expect(translate("zh", "add.partsCount", { count: 1 })).toBe("1 个部件");
  expect(translate("en", "composition.positionsUnit", { count: 1 })).toBe("position");
  expect(translate("en", "composition.positionsUnit", { count: 3 })).toBe("positions");
});

test("按语言格式化日期与今日", () => {
  const noon = new Date(2026, 9, 4, 12, 0, 0);
  expect(formatDate(noon, false, "zh")).toBe("2026/10/04");
  expect(formatDate(noon, false, "en")).toBe("10/04/2026");
  expect(formatDate(noon, true, "zh")).toContain("10/04");
  expect(formatDate(noon, true, "en")).toContain("10/04");
  expect(formatDate("", false, "zh")).toBe("时间未记录");
  expect(formatDate("", false, "en")).toBe("Time not recorded");
  expect(formatDate("not-a-date", false, "en")).toBe("not-a-date");
  expect(formatToday("zh", noon)).toContain("10月");
  expect(formatToday("en", noon)).toContain("October");
});

test("读取存储语言失败或未知值回退中文", () => {
  expect(readStoredLang({ getItem: () => "en" })).toBe("en");
  expect(readStoredLang({ getItem: () => "zh" })).toBe("zh");
  expect(readStoredLang({ getItem: () => "fr" })).toBe("zh");
  expect(readStoredLang({ getItem: () => { throw new Error("denied"); } })).toBe("zh");
});

test("describeError 按错误码生成本地化提示", () => {
  expect(describeError({ code: "invalid_id" }, tEn)).toBe(messages.en["common.invalidId"]);
  const http = describeError({ code: "api", status: 500, invalidJson: true }, tEn);
  expect(http).toContain("HTTP 500");
  expect(http).not.toMatch(/[\u4e00-\u9fff]/);
  expect(describeError({ code: "api", status: 400, detail: "服务器拒绝", copied: true }, tEn)).toBe(`服务器拒绝${messages.en["common.copiedSuffix"]}`);
  expect(describeError({ code: "bad_payload", status: 200 }, tEn)).toBe(messages.en["common.badPayload"].replace("{status}", "200"));
  expect(describeError(new Error("boom"), tEn)).toBe("boom");
  expect(describeError(undefined, tEn)).toBe(messages.en["common.unknownError"]);
});

test("本地校验错误按当前语言重新翻译", () => {
  const error = localizedError("add.errorNoContent");
  expect(error.code).toBe("i18n");
  expect(describeError(error, tEn)).toBe(messages.en["add.errorNoContent"]);
  expect(describeError(error, key => translate("zh", key))).toBe(messages.zh["add.errorNoContent"]);
  expect(describeError(localizedError("add.partsCount", { count: 2 }), tEn)).toBe("2 parts");
});

test("两种语言键集合一致", () => {
  expect(Object.keys(messages.en).sort()).toEqual(Object.keys(messages.zh).sort());
});

test("英文文案不含中文且占位符与中文一致", () => {
  for (const key of Object.keys(messages.zh)) {
    expect(messages.en[key], `missing en: ${key}`).toBeTruthy();
    expect(messages.en[key], `CJK in en: ${key}`).not.toMatch(/[\u4e00-\u9fff]/);
    expect(placeholders(messages.en[key]), `placeholders differ: ${key}`).toEqual(placeholders(messages.zh[key]));
  }
});

test("代码引用的翻译键都在两种语言中存在", () => {
  const used = new Set();
  for (const [path, source] of Object.entries(sources)) {
    if (path.includes(".test.")) continue;
    for (const match of source.matchAll(/\b(?:t|localizedError)\(\s*"([^"]+)"/g)) used.add(match[1]);
    for (const match of source.matchAll(/\b(?:labelKey|hintKey|titleKey|eyebrowKey)\s*:\s*"([^"]+)"/g)) used.add(match[1]);
    for (const match of source.matchAll(/\bkey\s*:\s*"([a-zA-Z][\w]*\.[\w.]+)"/g)) used.add(match[1]);
  }
  for (const match of i18nSource.matchAll(/\bt\(\s*"([^"]+)"/g)) used.add(match[1]);
  for (const match of i18nSource.matchAll(/\btranslate\(\s*\w+\s*,\s*"([^"]+)"/g)) used.add(match[1]);
  expect(used.size).toBeGreaterThan(450);
  expect([...used].filter(key => !(key in messages.zh) || !(key in messages.en))).toEqual([]);
});
