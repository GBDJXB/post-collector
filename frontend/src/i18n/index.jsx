import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import en from "./messages.en.js";
import zh from "./messages.zh.js";

export const LANG_STORAGE_KEY = "pc.lang";
export const messages = { zh, en };

const dateFormatters = {
  zh: {
    date: new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }),
    dateTime: new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }),
    today: new Intl.DateTimeFormat("zh-CN", { weekday: "long", month: "long", day: "numeric" }),
  },
  en: {
    date: new Intl.DateTimeFormat("en-US", { year: "numeric", month: "2-digit", day: "2-digit" }),
    dateTime: new Intl.DateTimeFormat("en-US", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }),
    today: new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric" }),
  },
};

const fallback = {
  lang: "zh",
  setLang: () => {},
  t: (key, values) => translate("zh", key, values),
};

const I18nContext = createContext(fallback);

export function normalizeLang(value) {
  return value === "en" ? "en" : "zh";
}

export function readStoredLang(storage) {
  try {
    const store = storage ?? globalThis.localStorage;
    return normalizeLang(store?.getItem(LANG_STORAGE_KEY));
  } catch {
    return "zh";
  }
}

export function translate(lang, key, values = {}) {
  const locale = normalizeLang(lang);
  const pluralKey = values.count === 1 ? `${key}.one` : key;
  const template = messages[locale][pluralKey] ?? messages[locale][key] ?? messages.zh[pluralKey] ?? messages.zh[key] ?? key;
  return Object.entries(values).reduce((text, [name, value]) => text.split(`{${name}}`).join(String(value ?? "")), template);
}

// 本地校验类错误只保存键名，随界面语言重新翻译。
export function localizedError(key, values) {
  const error = new Error(key);
  error.code = "i18n";
  error.key = key;
  error.values = values;
  return error;
}

export function formatDate(value, withTime = false, lang = "zh") {
  if (!value) return translate(lang, "date.unknown");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const formatter = dateFormatters[normalizeLang(lang)];
  return (withTime ? formatter.dateTime : formatter.date).format(date);
}

export function formatToday(lang, value = new Date()) {
  return dateFormatters[normalizeLang(lang)].today.format(value);
}

export function describeError(error, t) {
  if (!error) return t("common.unknownError");
  if (error.code === "invalid_id") return t("common.invalidId");
  if (error.code === "i18n") return t(error.key, error.values);
  if (error.code === "bad_payload") return t("common.badPayload", { status: error.status });
  if (error.code === "api") {
    const base = error.invalidJson
      ? t("common.httpFailure", { status: error.status })
      : error.detail ? String(error.detail) : t("common.invalidInput");
    return error.copied ? base + t("common.copiedSuffix") : base;
  }
  return error.message || t("common.unknownError");
}

export function I18nProvider({ children }) {
  const [lang, setLangState] = useState(() => readStoredLang());
  useEffect(() => {
    document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
    try {
      globalThis.localStorage?.setItem(LANG_STORAGE_KEY, lang);
    } catch {
      // 隐私模式或存储被禁用时保持当前会话语言，不阻断界面。
    }
  }, [lang]);
  const setLang = useCallback(value => setLangState(normalizeLang(value)), []);
  const value = useMemo(() => ({
    lang,
    setLang,
    t: (key, values) => translate(lang, key, values),
  }), [lang, setLang]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  return useContext(I18nContext);
}
