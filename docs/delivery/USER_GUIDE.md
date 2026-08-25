# Paper Reviewer 用户使用说明

本文面向使用 Windows 桌面端进行本科论文预评测的教师、指导教师和高校质量管理人员。它描述当前版本实际提供的功能；命令、路径和界面文字以当前发布包为准。

> 本软件是 AI 辅助预评工具，不是浙江省教育厅正式抽检系统。报告不能替代教师判断、学校审核、查重检测或正式抽检结论。当前 Rubric 标记为实验性，未完成教育测量效度验证，不得用于自动处分、学位决定或正式抽检认定。

## 1. 运行版本与启动

### 便携版

发布包是 PyInstaller `onedir` 目录。解压 `PaperReviewer-portable.zip` 后，双击解压目录根部的：

```text
PaperReviewer.exe
```

目标电脑不需要安装 Python。请保留 EXE 同目录中的 Qt 平台插件、配置、提示词、Rubric、Reviewer Profile、迁移文件和其他资源；不要只复制单个 EXE。运行时数据写入用户目录，不写入 EXE 目录。

### 开发环境

项目要求 Python 3.12（支持范围为 `>=3.12,<3.15`）。开发环境可使用：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install uv
.\.venv\Scripts\uv sync --extra dev
.\.venv\Scripts\paper-review-app.exe
```

开发专用控件画廊：

```powershell
.\.venv\Scripts\paper-review-gallery.exe
```

控件画廊不属于正式用户导航，也不用于启动评测。

## 2. 首次设置

在左侧导航打开“设置”。设置页包括 API Key、自定义 Provider、默认评测参数、外观和本地数据入口。

### 内置 Provider

当前可选项如下：

| 界面显示 | 协议 | 说明 |
| --- | --- | --- |
| OpenAI · Chat Completions | Chat Completions | 默认的 OpenAI 对话接口 |
| OpenAI · Responses API | Responses API | OpenAI Responses 接口；与 Chat 共用 OpenAI API Key |
| DeepSeek · Chat Completions | Chat Completions | DeepSeek 对话接口 |

在“API Key”区域分别保存 OpenAI 或 DeepSeek Key。OpenAI Chat 和 OpenAI Responses 共用同一个 OpenAI Key。桌面端保存后，Key 进入 Windows Credential Manager，不会写入 Provider 配置、任务快照、Trace、数据库或报告。若删除凭据且相应环境变量也不存在，新任务不能启动，依赖该 Key 的旧任务也不能恢复；重新保存后才可继续。

内置 Provider 在 CLI 和桌面应用服务中均可使用进程环境中的 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 作为兼容性回退，桌面端优先使用 Windows Credential Manager；`paper-review` CLI 会加载 `.env`，GUI 不会因此自动把 `.env` 中的 Key 注入进程。自定义 Provider 不使用环境变量回退。不要把 Key 写入 Git、截图、论文、报告或故障工单。

### 自定义 Provider

“设置”→“自定义 Provider”支持多个命名配置。点击“添加 Provider”，填写：

- 显示名称：活动配置不能重名（不区分大小写）。
- 接口协议：选择“Chat Completions”或“Responses API”。
- Base URL：填写 API 根路径，例如 `https://example.com/v1`；程序不会自动补充 `/v1`、`/responses` 或 `/chat/completions`。
- API Key：新建时必填；编辑时留空表示保留已有 Key。
- 默认模型：手工填写，不会从服务商拉取模型列表。

远程 Base URL 必须使用 HTTPS；HTTP 只允许 `localhost`、`127.0.0.1` 或 `[::1]`。不能包含用户名、密码、查询参数、片段或具体接口路径。自定义 Provider 的 Key 只保存到 Windows Credential Manager，并与 Provider ID、协议和端点指纹绑定。

已有配置编辑时，Base URL 和协议不可原地修改。需要更换时点击“更换端点/协议”，保存成功后系统创建新配置并归档旧配置；历史任务仍按创建时保存的端点恢复。归档配置默认保留，以便历史任务恢复；当前默认 Provider 不能直接归档。永久删除仅适用于没有历史任务引用的归档配置。

“测试兼容性”会先要求确认，然后发送一次可能计费的最小工具调用请求。测试不自动保存请求或响应内容；失败不阻止保存 Provider。在服务商返回相应元数据时，结果会安全显示：

- 服务商返回的 `message`、`code`、`param`（经过脱敏和长度限制）。
- `response.status`。
- `incomplete_details.reason`。
- `finish_reason`。
- 返回的 output item 类型。
- 是否只返回普通文本。

测试请求不应被当作免费探测。若服务商只返回文本、不支持工具调用，评测 Agent 通常无法按要求完成结构化输出。

### 默认参数与外观

在“默认评测参数”中可设置默认 Provider、默认模型、默认 Rubric 和“默认启用联网检索与参考文献自动核验”。“外观与可访问性”中可选择跟随系统、浅色、深色或高对比度主题，以及动画策略。设置保存成功会显示提示；保存失败时保留错误 Message Bar。

## 3. 新建评测

打开“新建评测”，按照页面从上到下填写：

1. **专业名称**：必填，例如“计算机科学与技术”。它用于选择专业评阅上下文。
2. **论文 PDF**：选择或拖放一个 PDF。当前只支持可搜索文本的普通本科论文 PDF；不支持设计作品、图纸、特殊培养成果、涉密论文和没有 OCR 的扫描件。
3. **Rubric**：选择 YAML。选中后立即进行结构化校验，并显示名称、版本、Schema、适用学历、评分状态、维度、权重、锚点和硬性规则。
4. **专业培养目标**：可选 YAML。文件必须存在、扩展名为 `.yaml/.yml`，并能按 UTF-8 读取；当前版本不会进一步验证其中的 YAML 语法或教育学有效性。没有时仍可以使用 Rubric 默认上下文。
5. **Provider 与模型**：选择内置或活动自定义 Provider，模型名称可手工填写。自定义 Provider 的协议和端点不能在单次任务中临时覆盖。
6. **外部检索**：可选择联网检索并核验参考文献。启用后可能访问 DDGS、OpenAlex、Crossref 和 arXiv 等公共服务；关闭后不会访问这些学术检索服务，但云端模型 Provider 仍会发起网络请求；本地回环 Provider 只连接本机端点。
7. **云端处理**：必须确认拥有处理该论文的授权。
8. **材料属性**：必须确认论文不包含涉密材料。

“添加查重/学术不端检测报告”是后续版本预留入口。当前点击只显示说明，不打开文件选择器，不读取、保存、上传或让模型读取检测报告。可以在线下查看检测报告，并在报告页人工复核的理由中记录核查结论，但不要上传文件。

只有配置有效、Provider 已有 Key、已选择一个存在的 `.pdf` 文件、专业名称和两项安全确认均完成后，“开始评测”才可用。PDF 是否损坏、加密、为无 OCR 扫描件或文本过少，会在任务开始后的解析阶段进一步检查。点击后按钮进入忙碌状态，避免重复创建任务。

### 评测过程

任务详情页会按阶段显示：解析论文、收集外部证据、专业化评分、确定性审计、独立专家面板、Meta 评语、报告验证与生成。阶段完成状态不等同于模型请求百分比；当前阶段可能显示不确定进度动画。

任务记录会保存任务 ID、论文名、Rubric 版本、Provider 显示名/协议、模型、状态和更新时间。任务产生的检查点会在每个阶段和 Reviewer 完成后保存。

## 4. 取消、恢复和任务记录

评测中点击“取消评测”，确认后后台任务会安全停止并保存已完成检查点。按钮可能在网络请求结束或线程安全停止后才恢复；窗口仍可操作。取消不会删除论文、报告或已完成检查点。

在“任务记录”中可按状态筛选、按论文名搜索，双击任务查看详情。可恢复失败、取消或旧版本门禁任务可点击“恢复评测”；致命失败不会提供自动恢复。恢复会读取任务创建时的 Rubric、Reviewer Profile、Provider 快照和请求上下文，只重跑缺失阶段，不重复已经持久化的 Reviewer 结果。

自定义 Provider 和 Responses API 任务的恢复入口是桌面端；CLI 不创建或恢复这两类任务。旧版没有 Provider 快照的 OpenAI/DeepSeek 任务按历史 Chat Completions 规则恢复。

应用退出时，若有活动评测，会询问是否取消任务并退出；选择“否”会返回应用。首版不提供系统托盘后台运行。

## 5. 查看报告与人工复核

完成任务后，报告页显示论文标题、Rubric、Provider/协议和模型。浙江 Schema v2 报告包含：

- 九项 0–4 诊断评分、分组得分、加权贡献和实验性百分制总分。
- 政治方向、学术诚信等否决项状态及人工处理记录。
- 3 人初评和条件性 2 人复评的独立意见。
- Findings 的严重程度、指标、摘要、置信度、人工核查标记、论文页码和证据。
- Reviewer 分歧、人工核查、审计说明和确定性决策路径。

报告中的实验性百分制没有及格线，不直接决定风险结论。新版报告使用 Rubric 标题展示易懂中文；指标、分组、规则和专家机器 ID 被中文化或隐藏，论文证据的 `block_id`/`evidence_id` 仍可能显示以便追溯。

### 人工复核

AI 阶段完成后，任务可能显示“评测完成 · 待人工复核”。系统不会在评测中途弹出人工问题；完整预评报告先生成，之后在报告页顶部显示待办卡片。否决项支持“确认成立”和“确认不成立”；专家面板无法判断时支持“触发风险”和“未触发风险”。每项决定都必须填写复核人和理由。

保存人工决定只进行本地确定性重算和报告更新，不再次调用模型。全部待办完成后，任务转为已生成报告；仍有待办时风险结论保持“待定”。待定报告仍可查看和导出，但 Markdown/PDF 会醒目标注“人工复核未完成，结论待定”。

当前浙江 Schema v2 新报告始终注明；历史 Legacy 报告保持创建时的原始内容：

- 本结果不是浙江省教育厅正式抽检结论。
- 百分制和五级锚点是本项目自定义诊断规则。
- 学术不端检测报告未由系统自动读取。
- 模型置信度未经校准，不是统计概率。

## 6. 导出 Markdown 与 PDF

报告页提供“导出 Markdown”“导出 PDF”和“打开报告目录”。默认文件名为：

```text
<论文文件名>_AI辅助评测报告.md
<论文文件名>_AI辅助评测报告.pdf
```

默认目录优先使用上次成功导出的目录，否则使用 Windows“文档”目录。另存为对话框取消不会启动后台任务，也不会显示错误。目标文件已存在时，需要单独确认覆盖。

Markdown 是报告基准产物，已存在的规范 `report.md` 会逐字节导出；旧任务缺少该文件时，系统使用任务快照确定性重建。PDF 只消费同一 Markdown 快照，在本地生成，为白底、A4 纵向、可搜索文本，不调用 LLM、不访问外部网络、不加载远程图片。导出期间两个导出按钮都会禁用，窗口仍保持响应。

如果 PDF 无法确认中文字体，系统会取消导出以避免乱码。成功后 Message Bar 提供“打开文件”。导出不会改变任务状态、Trace、数据库或原报告。

## 7. 本地数据、日志与备份

桌面端数据根目录为：

```text
%LOCALAPPDATA%\PaperReviewer\PaperReviewer
```

主要位置：

| 位置 | 内容 |
| --- | --- |
| `data\paper-reviewer.db` | 任务记录和数据库检查点 |
| `runs\<RUN_ID>\` | 单个任务的 Rubric、Provider 快照、证据、检查点、报告和 `trace.jsonl` |
| `logs\paper-reviewer.log` | 桌面应用日志（可能没有与每个任务逐条对应的完整事件） |
| `config\preferences.json` | 非秘密界面偏好、默认参数和最近模型 |
| `config\providers.json` | 不含 API Key 的自定义 Provider 配置 |

Provider Key 位于 Windows Credential Manager，不在以上文件中。备份时可关闭应用后复制 `data`、`runs`、`logs` 和 `config`；不要复制或公开凭据库内容。恢复备份要求保留任务目录名称与数据库一致。单独复制 `report.md` 只能查看报告，不能保证任务可恢复。

如需反馈问题，优先从“帮助”→“打开日志目录”或“设置”→“打开日志目录”进入日志目录；任务级证据和事件在对应 `runs\<RUN_ID>` 目录。提交前请脱敏：不要提供 API Key、Bearer Header、论文正文、原始 Provider 响应、查重报告或包含正文的截图。

## 8. CLI 基本用法

CLI 适合开发、批处理和 OpenAI/DeepSeek Chat 任务；自定义 Provider、Responses API 的创建和恢复首版由桌面端负责。

```powershell
.\.venv\Scripts\paper-review init
.\.venv\Scripts\paper-review doctor
.\.venv\Scripts\paper-review rubric validate configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml
.\.venv\Scripts\paper-review profile validate configs/review_profiles/zhejiang_undergraduate_specialists_v1.yaml
```

开始评测时必须明确提供专业、云端授权和非涉密确认：

```powershell
.\.venv\Scripts\paper-review run paper.pdf `
  --provider openai `
  --model YOUR_MODEL_NAME `
  --discipline-name "计算机科学与技术" `
  --allow-cloud-processing `
  --non-classified
```

常用命令：

```powershell
.\.venv\Scripts\paper-review status RUN_ID
.\.venv\Scripts\paper-review resume RUN_ID
.\.venv\Scripts\paper-review report RUN_ID
```

CLI 的默认数据位置由 `Settings` 和环境变量决定，开发时可在 `.env` 中配置 `PAPER_REVIEW_DATABASE_URL`、`PAPER_REVIEW_RUNS_DIR` 等参数。CLI 的 `doctor` 只检查环境变量形式的内置 Key；桌面端设置页还会检查 Windows Credential Manager。

## 9. 限制与升级注意事项

- 只支持可搜索文本 PDF；扫描件需要先在外部完成 OCR，再导入可搜索 PDF。
- 外部检索依赖网络和公共服务；搜索失败通常只形成报告警告，不代表论文或引用一定正确。
- 自定义 Provider 必须实现所选协议和 Agent function tool calling；普通文本接口不等价于兼容 Provider。
- 学术诚信、政治方向和其他否决项不能由 AI 自动确认，必须由人工复核。
- 查重/学术不端报告入口当前不处理文件。
- 系统不保证模型自评置信度是概率，也不提供教育测量效度结论。
- 升级时不要删除 `%LOCALAPPDATA%\PaperReviewer\PaperReviewer`。旧 Rubric、旧任务、旧报告和数据库迁移会按快照与兼容规则读取；升级前建议关闭应用并备份数据目录。

更深入的架构、Provider 协议和 Rubric 约束见 [`docs/architecture.md`](../architecture.md)、[`docs/desktop-app.md`](../desktop-app.md)、[`docs/provider-contract.md`](../provider-contract.md) 和 [`docs/rubric-spec.md`](../rubric-spec.md)。
