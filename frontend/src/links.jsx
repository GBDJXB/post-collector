import React from "react";

const urlPattern = /https?:\/\/[^\s<>"'`]+/gi;
const punctuation = /[.,!?;:，。！？；：]$/;

export function safeWebUrl(value) {
  if (typeof value !== "string" || value.length > 2048 || /\s|[\x00-\x1f\x7f]|\p{Cf}|%(?![\da-fA-F]{2})/u.test(value)) return null;
  if (!/^https?:\/\//i.test(value)) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && url.hostname && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function LinkedText({ text }) {
  const nodes = [];
  let previous = 0;
  for (const match of text.matchAll(urlPattern)) {
    const start = match.index;
    let end = start + match[0].length;
    for (let index = start; index < end; index++) {
      if (/[，。！？；：]/.test(text[index]) || (")]}".includes(text[index]) && text.slice(start, index).split(text[index]).length >= text.slice(start, index).split({ ")": "(", "]": "[", "}": "{" }[text[index]]).length)) { end = index; break; }
    }
    while (end > start && punctuation.test(text[end - 1])) end--;
    const target = safeWebUrl(text.slice(start, end));
    if (!target) continue;
    if (start > previous) nodes.push(text.slice(previous, start));
    nodes.push(<a key={start} href={target} target="_blank" rel="noopener noreferrer">{text.slice(start, end)}</a>);
    previous = end;
  }
  if (previous < text.length) nodes.push(text.slice(previous));
  return <>{nodes}</>;
}
