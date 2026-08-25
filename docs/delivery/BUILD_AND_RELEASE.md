# 构建与发布指南

本文说明 Windows 便携版的构建、检查、打包、冒烟验证、回滚和数据兼容要求。发布目标是 PyInstaller onedir 目录：用户解压 ZIP 后直接双击 PaperReviewer.exe，目标电脑不需要安装 Python。

## 1. 构建前提

构建机需要 Windows、Python >=3.12,<3.15、PowerShell、Git 和可访问的 Python 包索引。推荐使用 Python 3.12。项目的开发依赖包含 PyInstaller、pytest、pytest-qt、Ruff、mypy 和测试适配器。

在仓库根目录执行：

    py -3.12 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install uv
    .\.venv\Scripts\uv.exe sync --extra dev
    Test-Path .\.venv\Scripts\pyinstaller.exe

最后一条应返回 True。若 .venv 已存在，先执行 uv sync --extra dev；不要把 .venv、缓存、数据库或个人任务目录复制进发布包。

构建使用仓库根目录的 paper-reviewer.spec 和 scripts/build_portable.ps1。spec 的入口是 src/paper_reviewer/gui/app.py，不是 CLI；它收集 PySide6 平台插件、Fluent Token/QSS、SVG 图标、Prompt、内置 Rubric/Profile、打印 CSS、Alembic 配置和迁移文件。

## 2. 构建便携版

标准构建命令：

    .\scripts\build_portable.ps1

脚本会执行以下动作：

1. 使用 .venv\Scripts\pyinstaller.exe --noconfirm --clean paper-reviewer.spec 构建 onedir 目录。
2. 启动打包后的 EXE，依次运行四个自检参数。
3. 四项自检全部返回 0 后，使用 Compress-Archive 生成 ZIP。

构建产物为：

    dist\PaperReviewer\PaperReviewer.exe
    dist\PaperReviewer\...                 便携版依赖、Qt 插件和资源
    dist\PaperReviewer-portable.zip         便携版 ZIP，解压后直接运行

ZIP 是 dist\PaperReviewer\* 的内容归档，解压后应检查 PaperReviewer.exe 位于解压目录根部。发布时应整体分发 dist\PaperReviewer 目录或 ZIP，不能只复制 EXE。

## 3. 四项打包自检

构建脚本执行的四个参数如下。也可以在构建完成后逐项手动运行：

    $exe = Resolve-Path .\dist\PaperReviewer\PaperReviewer.exe
    $p = Start-Process -FilePath $exe -ArgumentList "--self-test-credentials" -Wait -PassThru -WindowStyle Hidden
    $p.ExitCode
    $p = Start-Process -FilePath $exe -ArgumentList "--self-test-database" -Wait -PassThru -WindowStyle Hidden
    $p.ExitCode
    $p = Start-Process -FilePath $exe -ArgumentList "--self-test-resources" -Wait -PassThru -WindowStyle Hidden
    $p.ExitCode
    $p = Start-Process -FilePath $exe -ArgumentList "--self-test-report-export" -Wait -PassThru -WindowStyle Hidden
    $p.ExitCode

四项都必须为 0：

| 参数 | 检查内容 |
| --- | --- |
| --self-test-credentials | 在系统凭据管理器中使用随机临时凭据完成写入、读取和删除；不触碰用户现有 API Key。 |
| --self-test-database | 创建、查询并释放临时 SQLite/aiosqlite 数据库，验证 SQLAlchemy SQLite 方言被打包。 |
| --self-test-resources | 检查 Prompt、Fluent QSS/Token、报告打印 CSS、必需 SVG、内置配置和 0003_evaluation_persistence.py。 |
| --self-test-report-export | 用真实 Qt QTextDocument + QPdfWriter 生成中文多页 A4 PDF，再用 PyMuPDF 重开，检查 %PDF-、页数、中文文本和免责声明。 |

自检失败时不要发布 ZIP。先保留命令行退出码和 Windows 事件/日志信息，检查 dist\PaperReviewer 是否完整。报告 PDF 自检如果提示没有可用中文字体，应在构建/运行环境安装或启用 Microsoft YaHei UI、Microsoft YaHei 或 SimSun；应用会拒绝生成可能乱码的 PDF，不应通过删除字体检查来“修复”。

## 4. 发布前代码与资源检查

在构建前确认工作区只包含预期源码、测试和文档变更：

    git status --short --branch
    git diff --check
    git rev-parse --short HEAD

确认版本和依赖来源：

    Get-Content .\pyproject.toml
    Get-Content .\uv.lock -TotalCount 20

发布前至少执行：

    .\.venv\Scripts\ruff.exe check src tests
    .\.venv\Scripts\mypy.exe src/paper_reviewer
    .\.venv\Scripts\python.exe -m pytest

这些命令的真实数量、耗时和失败项目必须记录到交付验收报告，不得用预估值代替。若只做打包冒烟而未执行完整测试，应明确标记未执行范围和风险。

资源核对重点：

- configs/rubrics/ 与 configs/review_profiles/ 的默认 YAML 已通过 CLI validate。
- src/paper_reviewer/agents/prompts/、src/paper_reviewer/gui/resources/ 和 src/paper_reviewer/reporting/resources/report_print.css 存在。
- migrations/versions/ 至少包含当前仓库的 0001_initial.py、0002_run_scoped_identifiers.py 和 0003_evaluation_persistence.py。
- paper-reviewer.spec 的 hiddenimports 和 datas 与当前代码、Provider、数据库、PDF 导出和 Credential Manager 需求一致。
- EXE 不应携带 .env、paper-reviewer.db、runs/、日志、API Key、论文 PDF 或测试响应。

## 5. 无 Python 的 Windows 冒烟测试

在另一台未安装 Python 的 Windows 10/11 环境中，解压 dist\PaperReviewer-portable.zip 到用户有写权限的目录。不要从只读的 Program Files 或压缩包内部运行。

建议按以下顺序验证：

1. 双击 PaperReviewer.exe，确认主窗口、原生标题栏、中文导航和状态栏显示正常。
2. 打开“设置”，验证内置 Provider、Credential Manager 和默认 Rubric 页面可以加载。
3. 创建或选择一个测试用自定义 Provider，确认 Base URL、协议、模型和兼容性测试状态显示正确；不要使用涉密论文或真实敏感响应做冒烟。
4. 在“新建评测”选择可搜索文本的测试 PDF，填写专业名称，确认云端授权和非涉密选项后启动评测。
5. 在运行中切换页面并点击取消，确认窗口仍响应，任务状态和检查点被保留。
6. 从任务记录恢复一个中断任务，确认已完成的 Reviewer 不重复调用。
7. 打开已完成报告，分别导出 Markdown 和 PDF，确认 PDF 为白底 A4、中文可搜索，且导出只读取本地报告快照，不触发模型或外部网络请求。
8. 检查“打开日志目录”路径可访问；兼容性测试诊断应只显示白名单元数据。运行错误、日志、Trace 和界面技术详情仍须人工检查，不能直接假定没有 API Key、Bearer Header、论文正文或原始 Provider 响应。

这类真实 EXE 测试不能由开发机上的 pytest 代替。网络 Provider 的真实请求可能计费，若无测试额度则使用本机回环的兼容服务，并记录测试协议、模型、是否调用工具和是否触发重试。

## 6. 数据、迁移和升级

桌面端运行数据默认位于 %LOCALAPPDATA%\PaperReviewer\PaperReviewer：

    data\paper-reviewer.db
    runs\<run_id>\
    logs\paper-reviewer.log
    config\preferences.json
    config\providers.json

升级前关闭 EXE，并备份整个 PaperReviewer 目录。数据库与对应 runs 任务目录必须成套备份，因为报告和检查点同时使用 SQLite 与任务目录 Artifact。

应用启动时会对 SQLite 执行兼容升级；Alembic 脚本用于可追踪的迁移管理：

    .\.venv\Scripts\alembic.exe -c alembic.ini upgrade head

在生产数据上运行 Alembic 前先复制数据库并确认 alembic.ini 的 sqlalchemy.url 指向目标库。当前运行时还要兼容没有 alembic_version 表的旧数据库，不能仅因为 Alembic 状态为空就覆盖或重建数据库。0002_run_scoped_identifiers 的 downgrade 明确禁止，因为可能合并合法的跨任务标识并造成数据丢失。

旧任务兼容边界：

- 缺少 provider.json 的旧 OpenAI/DeepSeek 任务按历史 Chat Completions 规则恢复。
- 缺少 request-context.json、panel-profile.json 或 report-presentation.json 时使用对应的安全兼容回退。
- Schema v1、未计分任务、旧状态值和已有 report.md 必须可读；历史报告不会被自动改写为新版中文展示。
- 自定义 Provider、Responses API 任务依赖任务中的 Provider 快照，归档或重命名活动配置不应改变旧任务端点。

## 7. 校验、清单与哈希

发布目录和 ZIP 生成后计算 SHA-256：

    Get-FileHash .\dist\PaperReviewer-portable.zip -Algorithm SHA256
    Get-FileHash .\dist\PaperReviewer\PaperReviewer.exe -Algorithm SHA256

记录以下信息到发布验收报告：

- Git commit（git rev-parse HEAD）和项目版本（pyproject.toml）。
- Windows 版本、Python/uv/PySide6/PyInstaller 版本（构建机）。
- ZIP、EXE 字节数和 SHA-256。
- Ruff、mypy、pytest 命令及实际结果。
- 四项打包自检的退出码。
- 无 Python Windows 冒烟测试环境和未执行项目。

不要在文档、日志或发布备注中记录 API Key、Cookie、完整 Authorization Header、论文正文、Provider 原始响应、个人绝对路径或用户任务快照。发布清单可以写 %LOCALAPPDATA%\PaperReviewer\PaperReviewer 等 Windows 通用路径，不要写开发者机器的用户名路径。

## 8. 回滚

如果新版本启动或恢复失败：

1. 关闭新版本并保留日志、错误时间和非敏感的运行 ID。
2. 不删除或覆盖 %LOCALAPPDATA%\PaperReviewer\PaperReviewer；先复制备份。
3. 将便携目录替换为上一份已验证的 PaperReviewer 目录或 ZIP。
4. 使用上一版本打开同一份备份数据，先查看任务状态和 runs\<run_id> 检查点。
5. 若问题涉及数据库迁移，不执行未经验证的 downgrade；使用备份数据库和对应旧版本目录恢复。

构建目录 build/、发布目录 dist/、build-*、dist-*、.venv/、数据库、runs/、.env 和日志不属于源码交付件。它们应由 .gitignore 忽略，也不能为了清理仓库而删除用户已有任务或凭据。

## 9. 发布后检查

把最终 ZIP 的下载/复制位置、SHA-256 和已知限制交给使用者；同时附上 USER_GUIDE.md、TROUBLESHOOTING.md 和 DEVELOPER_GUIDE.md。本项目仍是实验性 AI 辅助预评工具，浙江本科论文 Rubric 的教育测量效度尚未完成验证，发布版本不能声称等同于教育主管部门正式抽检。
