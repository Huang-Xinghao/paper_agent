---
name: paper-compile
description: 编译 LaTeX 论文、根据日志修复最小错误并验证 PDF 输出。
allowed-tools: Read, Edit, Grep, Glob, Bash
---

# 论文编译

读取 `paper/main.tex`、章节、图表包含文件、参考文献和 `paper/main.log`，使用项目运行器指定的 LaTeX 工具编译 `paper/main.pdf`。本 skill 只修复编译和排版问题，不改变研究结论、实验数字、数据、方法或引用事实。

## 编译流程

1. 先用 `bash` 检查 `latexmk`、所用 TeX 引擎和可用磁盘；缺少工具或 TeX 包时，按当前平台通过包管理器安装必要组件并验证版本。安装位置必须可被 runner 后续进程找到，不依赖仅当前 shell 生效的 PATH。权限、网络或磁盘原因导致安装失败时报告实际命令和错误，不重复盲试。再检查 `paper/main.tex`、`paper/sections/*.tex`、`paper/references.bib` 和引用的图文件存在。
2. 运行 runner 配置的编译命令；不要在 skill 中声称编译成功，成功由 runner 检查 PDF 魔数和文件完整性。
3. 失败时读取日志中的第一个可定位错误，在当前工具调用循环中修改最小相关文件并重新编译。检查缺失包、图路径、引用键、未定义标签、BibTeX 语法和严重 overfull box。
4. 编译修复使用当前 Agent 调用的统一工具轮次预算；runner 的结束校验失败时会将错误反馈到同一段对话。预算耗尽后保留日志和准确的错误、文件及行号，不进行无关重构。

## 编译后检查

由 runner 验证 `paper/main.pdf` 存在且可读。检查日志中的 undefined citation/reference、缺失图和 LaTeX 错误；页数只按 `PAPER_PLAN.md` 的 venue 预算报告，不通过压缩字体或删除证据掩盖超页。不得伪造 PDF、结果或引用；依赖安装仅用于补齐当前论文的编译环境。
