# 项目文档与审阅记录

使用手册见 [README](../README.md)，初版设计契约见 [DESIGN](../DESIGN.md)，开发约束见 [AGENTS](../AGENTS.md)。

## 安全审计原始报告

[441d41b 安全审计](reviews/441d41b-security-audit/SUMMARY.md)针对提交 `441d41b7b2e06f8332a99d7a9ef610633803fdd6`。文件从原 `.pentest-out/` 目录整理而来，报告正文保持原样。

这些文件记录审计工具当时的结论和复现方法。本次目录整理没有逐条核验漏洞，不代表维护者已确认其严重性或修复状态。报告中的工作区路径、目录名称和环境限制保留为历史记录，当前文件位置以本页链接为准。

| 文件 | 用途 |
| --- | --- |
| [SUMMARY.md](reviews/441d41b-security-audit/SUMMARY.md) | 审计摘要与报告提出的优先级 |
| [pentest-report.md](reviews/441d41b-security-audit/pentest-report.md) | 完整报告与逐项复现步骤 |
| [git-version.txt](reviews/441d41b-security-audit/git-version.txt) | 审计代码基线 |
| [reproduction-index.md](reviews/441d41b-security-audit/evidence/reproduction-index.md) | 复现索引 |
| [env-limitations.md](reviews/441d41b-security-audit/evidence/env-limitations.md) | 当时的环境与验证限制 |

后续核验和修复应另写记录，保留原报告以供对照。
