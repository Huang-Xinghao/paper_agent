---
name: paper-compile
description: "将 LaTeX 论文编译为 PDF ，修复错误并验证输出。当用户说“编译论文”、“编译论文”、“构建 PDF ”、“生成 PDF ”，或者想要将 LaTeX 编译为可提交的 PDF 时使用。"
argument-hint: "[paper-directory]"
allowed-tools: Bash(*), Read, Write, Edit, Grep, Glob
---

# 论文编译：LaTeX 至提交就绪 PDF

编译 LaTeX 论文并修复任何问题：**$ARGUMENTS**

## 常量

- **COMPILER = `latexmk`** — LaTeX 构建工具。自动处理多遍编译。
- **ENGINE = `pdflatex`** — LaTeX 引擎。选项：`pdflatex`（默认）、`xelatex`（对于 CJK/custom 字体）、`lualatex` 。
- **MAX_COMPILE_ATTEMPTS = 3** — 修复错误和重新编译的最大尝试次数。
- **PAPER_DIR = `paper/`** — 包含 LaTeX 源文件的目录。
- **MAX_PAGES** — 页数限制。机器学习会议：主体到结论结束（不包括参考文献和附录）。 ICLR=9，NeurIPS=9，ICML=8。 **IEEE 会议或期刊：参考文献包含在页数中。** IEEE 期刊 ≈ 12-14 页，IEEE 会议 ≈ 5-8 页（全部包含）。

## 工作流程

### 第 1 步：验证先决条件

检查编译环境是否已准备好：

```bash
# 检查LaTeX安装情况
which pdflatex && which latexmk && which bibtex

# 如果未安装，请提供说明：
# macOS：brew install --cask mactex-no-gui
# Ubuntu：sudo apt-get install texlive-full
# 服务器：conda install -c conda-forge texlive-core
```

验证所有必需的文件是否存在：

```bash
# 必须存在
ls $PAPER_DIR/main.tex

# 应该存在
ls $PAPER_DIR/references.bib
ls $PAPER_DIR/sections/*.tex
ls $PAPER_DIR/figures/*.pdf 2>/dev/null || ls $PAPER_DIR/figures/*.png 2>/dev/null
```

### 第 2 步：第一次编译尝试

```bash
cd $PAPER_DIR

# 清理之前的构建产物
latexmk -C

# 完整编译（pdflatex + bibtex + pdflatex × 2）
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex 2>&1 | tee compile.log
```

### 第 3 步：错误诊断和自动修复

如果编译失败，请阅读`compile.log`并修复常见错误：

**缺少包裹：**
```
！  LaTeX 错误：找不到文件“somepackage.sty”。
```
→ 通过 `tlmgr install somepackage` 安装或移除 `\usepackage`（如果未使用）。

**未定义的参考：**
```
LaTeX 警告：第 3 页上的引用“fig:xyz”未定义
```
→ 检查 `\label{fig:xyz}` 是否存在于正确的图形环境中。

**缺失数字：**
```
！  LaTeX 错误：找不到文件“figures/fig1.pdf”。
```
→ 检查文件是否存在具有不同扩展名的文件（.png 与 .pdf）。更新 `\includegraphics` 路径。

**引用未定义：**
```
LaTeX 警告：引用“smith2024”未定义
```
→ 将缺失的条目添加到 `references.bib` 或修复引文键。

**文本中的 `[VERIFY]` 标记：** → 搜索 `/paper-write` 留下的 `[VERIFY]` 标记。这些表明未经证实的引文或事实。为用户搜索正确的信息或标志。

**水平箱过满：**
```
段落第 42--45 行的 \hbox 过满（太宽 12.5pt）
```
→ 轻微：通常可以忽略。如果严重（>20pt），请重新措辞文本或调整图形宽度。

**BibTeX 错误：**
```
我期待的是 `,' or a ` }'---references.bib 的第 15 行
```
→ 修复 BibTeX 语法（缺少逗号、不匹配的大括号、标题中的特殊字符）。

**`\crefname` 未定义自定义定理类型：** → 确保 `\crefname{assumption}{Assumption}{Assumptions}` 和类似内容位于 `\newtheorem{assumption}` 之后的序言中。

### 第 4 步：迭代修复循环

```
尝试 1.. MAX_COMPILE_ATTEMPTS ：
    编译（）
    如果成功：
        休息
    解析错误（）
    自动修复（）
```

对于每个错误：
1. 从 `compile.log` 读取错误消息
2. 找到源文件和行号
3. 应用修复
4. 重新编译

**两次尝试后卡住？** 如果安装了 Codex 插件，则调用 `/codex:rescue` — Codex 可以独立读取 LaTeX 源代码和 `compile.log` 来发现 Claude 遗漏的问题（例如，冲突的包、编码问题、细微的宏错误）。如果没有安装，继续Claude自己的诊断。

### 第 5 步：编译后检查

编译成功后，验证输出：

```bash
# 检查 PDF 是否存在并且有内容
ls -la main.pdf
# 检查页数
pdfinfo main.pdf | grep Pages

# macOS：开放进行目视检查
# 打开main.pdf
```

**视觉审稿（自动）：** 如果编译后的PDF存在，直接阅读查看视觉传送方式：
- 图形质量：可读的标签、清晰的文本、可区分的颜色
- 布局：没有孤立的节标题，没有尴尬的分页符
- 图形出现在其第一个文本参考附近（而不是页面之外）
- 表格：列对齐，小数精度一致
- 没有过多的内容明显超出了过去的边距

这是一个快速的视觉审稿，而不是完整的审稿——改进循环确实使视觉审稿更加深入。

**自动检查：**

- [ ] PDF 文件存在且 > 100KB（不是 empty/corrupt ）
- [ ] 总页数合理（MAX_PAGES + 附录 + 参考文献）
- [ ] 不 ”？？”在 PDF 中（未定义的引用 — grep 日志）
- [ ] PDF 中没有“[?]”（未定义的引用 — grep 日志）
- [ ] 图形已渲染（不缺少图像占位符）

```bash
# 检查未定义的引用
grep -c "LaTeX Warning.*undefined" compile.log

# 检查是否有缺失的引用
grep -c "Citation.*undefined" compile.log
```

### 第 6 步：页数验证

**关键**：验证论文是否适合 MAX_PAGES 。

**对于 ML 会议 ( ICLR/NeurIPS/ICML/CVPR/ACL/AAAI )：** 正文 = 第一页到结论部分末尾（不一定是 §5 - 可能是 §6、§7 或 §8，具体取决于结构）。参考文献和附录不计算在内。

**对于 IEEE 会议或期刊：** 总页数（包括参考文献）必须在限制范围内。没有单独的“主体”计数——直到并包括引用的所有内容都计数。

**使用 `pdftotext` 进行精确检查：**
```bash
# 提取文本并找到结论结束与参考文献开始的位置
pdftotext main.pdf - | python3 -c "
import sys
text = sys.stdin.read()
pages = text.split('\f')
for i, page in enumerate(pages):
    if 'Ethics Statement' in page or 'Reproducibility' in page:
        print(f'Conclusion ends on page {i+1}')
    if any(w in page for w in ['References', 'Bibliography']):
        lines = [l for l in page.split('\n') if l.strip()]
        for l in lines[:3]:
            if 'References' in l or 'Bibliography' in l:
                print(f'References start on page {i+1}')
                break
"
```

如果结论在页中结束并且参考文献在同一页上开始，则主体就是该页码（例如，如果两者都在第 9 页上，则主体 = ~8.5 页，这对于 9 页的限制来说很好，因为它为参考文献标题留出了空间）。

如果超过限制：
- 确定哪些部分最长
- 建议具体删减（将说明移至附录、压缩表格、加强写作）
- 报告：“正文为X页（限制：MAX_PAGES）。建议：将[具体内容]移至附录。”

### 步骤 6.5：过时文件检测

检查 `main.tex` 未引用的孤立节文件：

```bash
# 查找 sections/ 中的所有 .tex 文件并检查哪些是由 main.tex 输入的
for f in paper/sections/*.tex; do
    base=$(basename "$f")
    if ! grep -q "$base" paper/main.tex; then
        echo "WARNING: $f is not referenced by main.tex — consider removing"
    fi
done
```

这可以防止当节结构发生变化时剩余文件造成混乱（例如，重组为 7 个节后留下的旧 `5_conclusion.tex`）。

### 第 7 步：提交准备

对于会议提交，额外检查：

- [ ] **匿名**：没有显示身份的作者姓名、从属关系或自引
- [ ] **页数限制**：MAX_PAGES内的正文（到结论结束）
- [ ] **字体嵌入**：PDF 中嵌入的所有字体
  ```bash
  pdffonts main.pdf | grep -v "yes"  # 不应返回任何内容（或仅返回标头）
  ```
- [ ] **无补充混入**：`\newpage\appendix`后明确附录
- [ ] **文件大小**：合理（大多数会议或期刊 < 50MB，最好 < 10MB）
- [ ] **没有 `[VERIFY]` 标记**：搜索 PDF 文本中剩余的标记

### 第 8 步：输出摘要

```markdown
## 编译报告

- **状态**：成功/失败
- **PDF**:  paper/main.pdf
- **页数**：X（正文到结论）+ Y（参考文献）+ Z（附录）
- **页数限制内**：YES/NO (MAX_PAGES = N)
- **已修复的错误**：[自动修复问题列表]
- **剩余警告**：[非严重警告列表]
- **未定义的参考**：0
- **未定义的引用**：0

### 下一步
- [ ] PDF 目视检查
- [ ] 运行 `/paper-write` 来修复任何内容问题
- [ ] 通过 OpenReview / CMT / HotCRP 提交至 [目标会议或期刊]
```

## 主要规则

- **永远不要删除用户的源文件** — 仅修改以修复错误
- **保留compile.log**——对于调试很有用
- **不要抑制警告** - 报告警告，让用户决定
- **如果未安装 LaTeX**，请提供清晰的安装说明，而不是默默地失败
- **字体嵌入至关重要** — 一些会议或期刊拒绝使用非嵌入字体的 PDF
- **页数规则因目标会议或期刊而异** — ML 会议：主体到结论（不包括参考文献）。 **IEEE 会议或期刊：包括参考文献在内的总页数。**

## 共同目标会议或期刊要求

| 目标会议或期刊 | 样式文件 | 引文 | 页数限制 | 参考文献有限制吗？ | 提交 |
|-------|-----------|----------|------------|----------------|------------|
| ICLR 2026 | `iclr2026_conference.sty` | `natbib`  ( `\citep` / `\citet` ) | 9页（到结论结束） | 不 | 开放评审 |
| 神经IPS 2025 | `neurips_2025.sty` | `natbib`  ( `\citep` / `\citet` ) | 9页（到结论结束） | 不 | 开放评审 |
| ICML 2025 | `icml2025.sty` | `natbib`  ( `\citep` / `\citet` ) | 8页（到结论结束） | 不 | 开放评审 |
| IEEE期刊 | `IEEEtran.cls` [期刊] | `cite`（`\cite{}`，数字） | ~12-14 页（交易）/ ~4-5（信件） | **是的** | IEEE 作者门户 / ScholarOne |
| IEEE会议 | `IEEEtran.cls`【会议】 | `cite`（`\cite{}`，数字） | 5-8 页（因会议而异） | **是的** | EDAS / IEEE 作者门户 |

