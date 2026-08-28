# Course Paper Reviewer（课程论文批量评测版）

Course Paper Reviewer 是面向普通课程论文的 Windows 桌面评测工具。它使用课程 Rubric、
有边界的多 Reviewer Agent 和确定性程序校验，为一个文件夹中的 PDF 依次生成课程评分、
改进意见、个人报告及汇总表。

本分支为独立课程版：`codex/course-paper-reviewer`。本科毕业论文抽检版完整保留在
`main`，课程版不会覆盖或合并其用户数据、配置和凭据。

## 适用范围

- 按当前课程的任务和评价标准评测普通课程论文，不考察学生所属专业的培养目标或专业深度。
- 创建批次时不要求填写专业名称；专业只作为自动提取的本地元数据，用于报告和汇总表。
- 扫描所选文件夹顶层的 `1–100` 个 PDF，不递归读取子目录，并按文件名顺序逐篇评测。
- 支持停止、从检查点继续，以及只重试失败论文；已完成论文不会重复调用模型。
- 自动提取姓名、学号、专业和正文可见题目；PDF 隐藏标题只用于核对，不能单独成为题目。
- 低置信度或缺失字段会醒目标记，可逐篇核对，也可对历史批次执行完全本地的“一键重新检查”。
- 评分 Reviewer 和 Meta Reviewer 不接收姓名、学号、专业或原始文件路径；专用元数据提取步骤
  仍可能通过所选云端模型处理封面内容，因此开始前必须确认处理授权和非涉密属性。
- 每篇生成固定格式的 PDF 报告，并生成 UTF-8 BOM 编码的
  `课程论文评测汇总.csv`，方便用 Excel 打开。

## 默认评分规则

内置实验 Rubric 为 `configs/rubrics/course_paper_v1.yaml`：

| 评价维度 | 权重 |
| --- | ---: |
| 课程任务完成度 | 25% |
| 课程知识理解与运用 | 25% |
| 论证与证据 | 20% |
| 结构与逻辑 | 15% |
| 文字表达 | 10% |
| 引用格式规范 | 5% |

每项采用百分制五级锚点：`0–39` 核心任务明显缺失、`40–59` 完成不足、
`60–74` 达到基本要求、`75–89` 良好、`90–100` 优秀。总分按权重计算，
默认 `60` 分为“达到基本要求”的课程结论边界。

这是一套适当放宽的通用课程模板，不是所有课程的正式评分标准。正式使用前，教师应根据
课程大纲、作业要求和教学目标检查或替换 Rubric；不得仅凭 AI 结果直接形成学生正式成绩。

## 直接使用 Windows 便携版

1. 解压整个 `CoursePaperReviewer-portable.zip`，不要只复制其中的 EXE。
2. 双击 `CoursePaperReviewer\CoursePaperReviewer.exe`。
3. 在“设置”中配置 Provider、模型和 API Key，也可以先运行兼容性测试。
4. 在“新建批次”中选择论文文件夹和输出目录，确认云端处理授权、非涉密及个人信息输出风险。
5. 检查扫描预览和课程 Rubric 后，点击“开始批量评测”。
6. 在“批次记录/批次详情”中查看进度、停止、继续、重试失败项或核对论文信息。

课程批量流程目前只在桌面端提供，不新增 CLI 批量命令。

### 批次输出

默认报告名为：

```text
姓名_学号_专业_题目_课程论文评测报告.pdf
```

文件名会自动清理 Windows 非法字符并限制长度。姓名、学号或题目仍待核对时，首次报告使用：

```text
原文件名__待核对__任务ID前8位_课程论文评测报告.pdf
```

仅专业未识别时仍使用标准格式并显示“未识别专业”。人工确认后，报告会在本地重建并改为标准
姓名、学号、专业、题目格式；若出现重名，程序会追加稳定短标识，绝不覆盖已有报告。输出目录同时包含：

```text
课程论文评测汇总.csv
```

汇总表包含原文件名、姓名、学号、专业、题目、待核对字段、人工核对状态、各维度分数、总分、
等级、结论、状态、报告文件名和脱敏错误摘要。以 `= + - @` 开头的单元格会被安全转义，避免公式注入。

每个批次使用独立输出目录。自动建议的目录在提交批次后会立即轮换；如果手工选择了已有
`课程论文评测汇总.csv` 或批次归属标记的旧目录，程序会在开始前提示选择新目录，或前往
“批次记录”继续原批次，不会覆盖或误报为普通写入权限问题。

## Provider 与数据隔离

课程版支持 OpenAI Chat Completions、OpenAI Responses、DeepSeek 和 OpenAI-compatible
自定义 Provider。自定义 Provider 的协议在创建时固定为 Chat Completions 或 Responses。

首次启动且课程版尚无配置时，程序会自动尝试一次从本机论文版只读复制 Provider 目录和
Windows Credential Manager 凭据。复制完成后两套产品完全独立，后续修改互不影响。课程版不会从
`OPENAI_API_KEY` 或 `DEEPSEEK_API_KEY` 环境变量回退读取密钥。

运行数据位于：

```text
%LOCALAPPDATA%\CoursePaperReviewer\CoursePaperReviewer\
├─ data\       SQLite 数据库
├─ runs\       单篇任务、检查点和规范报告
├─ batches\    批次清单与恢复状态
├─ logs\       course-paper-reviewer.log
└─ config\     非秘密偏好和 Provider 目录
```

API Key 存放在 Windows Credential Manager 的 `CoursePaperReviewer` 服务命名空间中，
不会写入批次清单、数据库、Trace、报告或日志。用户选择的批次输出目录会包含学生个人信息，
应按学校的数据管理要求保存、传输和备份。

## 开发环境

要求 Python `3.12`（项目声明兼容 `>=3.12,<3.15`）。推荐使用项目虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install uv
.\.venv\Scripts\uv.exe sync --extra dev
```

启动课程桌面端：

```powershell
.\.venv\Scripts\course-paper-review-app.exe
```

质量检查：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest
```

## 构建便携版

```powershell
.\scripts\build_course_portable.ps1
```

脚本会构建 onedir 便携目录，依次运行凭据、SQLite、资源、Markdown/PDF、课程 Rubric/Profile、
批量命名/CSV 和 Qt GUI 启动自检，然后生成：

```text
dist-course\CoursePaperReviewer\CoursePaperReviewer.exe
dist-course\CoursePaperReviewer-portable.zip
```

目标 Windows 10/11 计算机无需安装 Python。`build-course/` 和 `dist-course/` 是本地构建产物，
不会提交到 Git。

## 文档与架构

- [课程版完整使用与交付指南](docs/delivery/COURSE_EDITION_GUIDE.md)
- [开发维护指南](docs/delivery/DEVELOPER_GUIDE.md)
- [常见报错对照](docs/delivery/TROUBLESHOOTING.md)
- [基础项目架构](docs/architecture.md)
- [Rubric 扩展规范](docs/rubric-spec.md)

底层仍采用 ports-and-adapters 架构：模型负责语义判断，Python Harness 负责工具权限、预算、
证据校验、状态、检查点、批次调度、持久化和报告生成。

## 重要声明

- 本工具输出是课程论文评分辅助，不自动成为教师正式成绩。
- 通用 Rubric 版本为 `0.1.0-experimental`，尚未完成具体课程的教育测量效度验证。
- 系统不自动认定抄袭、代写或其他学术不端，也不读取学校查重报告。
- 扫描件、加密或损坏 PDF、无法抽取文字的 PDF 可能无法评测，需要先转换为可搜索文本 PDF。
- 云端 Provider 会接收完成评测所需的论文内容；涉密、无授权或不允许上传的材料不得使用。
