# 测试与验收报告

> 本文件记录本次等价重构版本的实际自动化验收与打包结果。发布判定为有条件通过；未执行的人工、真实服务商和特定环境验收项目在第 9 节单独列出。

## 1. 报告状态

| 项目 | 当前记录 |
| --- | --- |
| 报告用途 | 等价重构版本的发布验收与回归记录 |
| 基线提交 | `1bee1ac` |
| 构建源码提交 | `eafd4482e239a4513821e4821da4c329f8fbdea4` |
| 验收日期 | 2026-08-26 |
| 验收人员 | 主代理终端记录（自动化与构建） |
| 发布判定 | 有条件通过 |

“有条件通过”表示已知自动化检查和打包自检没有高风险失败，但仍有未执行的人工、真实服务商或特定环境验收项目。

## 2. 验收环境

| 项目 | 版本或值 |
| --- | --- |
| 操作系统 | Windows 11 build 26200 |
| Python | 3.12.13 |
| PySide6 | 6.11.2 |
| PyInstaller | 6.22.2 |
| uv | 0.12.5 |
| Ruff | 0.16.4 |
| mypy | 2.3.1 |
| pytest | 9.1.1 |
| PyMuPDF | 1.28.2 |
| OpenAI SDK | 3.3.1 |
| 基线 Git HEAD | `1bee1ac` |
| 项目要求 | Python >=3.12,<3.15 |

## 3. 可复现的质量检查命令

以下命令在项目根目录、项目虚拟环境中执行：

    .\.venv\Scripts\python.exe -m ruff check src tests migrations
    .\.venv\Scripts\python.exe -m mypy src
    .\.venv\Scripts\python.exe -m pytest

最终记录为 Ruff 检查 `src tests migrations` 退出码 0；mypy 检查 `src` 的 101 个文件无问题；pytest 退出码 0，`283 passed in 56.20s`。分阶段结果如下：

| 阶段提交 | Ruff/mypy | pytest | pytest 耗时 |
| --- | --- | --- | --- |
| `71551e0` | 通过 | 257 passed | 400.06s |
| `b400bec` | 通过 | 273 passed | 402.18s |
| `89da6ee` | 通过 | 280 passed | 55.66s |
| `eafd448` | 通过 | 283 passed | 56.20s |

## 4. 自动化测试结果

### 4.1 静态检查与类型检查

| 检查 | 命令 | 结果 | 数量/耗时 |
| --- | --- | --- | --- |
| Ruff | `python -m ruff check src tests migrations` | 通过，exit 0 | 未单独记录耗时 |
| mypy | `python -m mypy src` | 通过，exit 0 | 101 files，无问题；未单独记录耗时 |
| 完整 pytest | `python -m pytest` | 通过，exit 0 | 283 passed in 56.20s |

最终自动化回归没有已知高风险失败；未执行的人工和外部环境项目不因自动化通过而视为完成。

### 4.2 等价性与核心流程

| 场景 | 必须验证的行为 | 实际结果 |
| --- | --- | --- |
| Schema v1 | 旧 Rubric 能加载、校验、运行和查看历史报告 | 最终 pytest 回归通过；未执行独立人工组合验收 |
| Schema v2 | 九项诊断评分、权重、否决项和严格字段校验保持有效 | 最终 pytest 回归通过 |
| 状态与事件 | 阶段、状态、事件顺序和中文展示没有非预期变化 | 最终 pytest 回归通过 |
| 3+2 专家面板 | 初评及条件性复评规则、专家意见和风险路径保持一致 | 最终 pytest 回归通过；未执行独立人工组合验收 |
| 人工复核 | 评测完成后再复核；复核只进行本地确定性刷新，不调用 LLM | 最终 pytest 回归通过 |
| 取消 | GUI 取消可响应，状态持久化为已取消，已完成检查点可保留 | 最终 pytest 回归通过；未在独立、未安装 Python 的 Windows 环境执行 EXE/UI 人工冒烟 |
| 恢复 | 中断或失败后恢复，不重复已持久化的 Reviewer/专家结果 | 最终 pytest 回归通过；新增安全回归覆盖恢复前 Provider 快照与 RunRecord 的 provider/model 一致性 |
| 历史任务 | 无新快照的旧 OpenAI/DeepSeek 任务仍按兼容规则恢复 | 最终 pytest 回归通过；独立旧任务组合验收未执行 |

### 4.3 Agent 与模型协议

| 场景 | 必须验证的行为 | 实际结果 |
| --- | --- | --- |
| Chat Completions | 消息、工具、预算、重试和调用次数与基线一致 | pytest 中的模拟/fixture 回归通过；未发真实付费请求 |
| Responses API | instructions、input、工具调用、store=False 和输出映射正确 | pytest 中的模拟/fixture 回归通过；未发真实付费请求 |
| Responses continuation | 工具输出的 call_id 正确；continuation 不持久化、不串 Reviewer | pytest 中的模拟/fixture 回归通过 |
| 截断与 repair | 截断、非法最终输出和 repair 上下文行为不变 | 最终 pytest 回归通过 |
| 自定义 Provider | 协议、端点、凭据指纹、归档和旧任务快照恢复保持隔离 | 自动化模拟回归通过；真实自定义 Provider 请求未执行 |
| 敏感信息 | API Key、Bearer Header、敏感 URL 和响应原文不进入正常持久化产物；兼容性诊断仅显示白名单字段；运行错误和 traceback 需人工检查 | 文档、源码和产物脱敏检查通过；运行错误/traceback 的人工日志复核仍是残余风险 |

协议回归使用测试模拟/fixture；本次未执行真实付费 Provider 请求，因此没有真实请求次数或服务商原始响应记录。

### 4.4 GUI、数据库、凭据和资源

| 检查 | 复现方式 | 实际结果 |
| --- | --- | --- |
| GUI 单元/Qt 测试 | `python -m pytest tests\gui` | 包含于最终 pytest 回归，283 passed |
| GUI 契约 | `python -m pytest tests\characterization\test_gui_contracts.py` | 包含于最终 pytest 回归，283 passed |
| 数据库 | 应用服务与 run-scoped persistence 测试 | 包含于最终 pytest 回归，283 passed |
| Windows 凭据 | keyring store 测试及 credentials 自检 | 自动化回归通过；源码与 EXE 自检均在当前 Windows Credential Manager 使用随机临时凭据完成写入、读取和删除 |
| 报告导出 | report export 测试及 EXE 自检 | 自动化回归通过；EXE report-export 自检 exit 0 |
| Rubric/评分 | rubric 与 scoring 测试 | 包含于最终 pytest 回归，283 passed |

GUI offscreen 采样 N=9：进程启动到 `MainWindow` 构造中位数 1.5165s，构造 374.509ms，`gui.app` import 163.627ms。该测量不含 `show`/首帧，也未清空页面缓存，不能解释为可见首帧性能。浅色/深色之外的高对比度、100%–200% 缩放、焦点顺序和完整人工 GUI 检查见第 9 节。

## 5. 报告与导出验收

报告导出 EXE 自检退出码为 0，覆盖确定性中文多页 Markdown 到 PDF 的本地渲染，并使用 PyMuPDF 重新打开文件，检查 PDF 签名、页数、可抽取中文文本和免责声明。报告快照重建及 Markdown 字节级导出由 pytest 覆盖，不属于该 EXE 自检的执行路径。规范 Markdown 作为报告基准，PDF 生成不调用 LLM、不访问外部网络、不加载外部图片或样式的自动化检查通过。

代表性长中文多页 PDF 的逐页 PNG 版式检查、本次单独 Markdown 哈希记录和完整人工复核尚未执行；因此不能把 PDF 的人工版式质量标为完整通过。待人工复核报告仍需显示“人工复核尚未完成，当前风险结论待定”。

## 6. 打包与 EXE 验收

构建命令为：

    .\scripts\build_portable.ps1

构建脚本成功，工具 wall time 约 119s。四项 EXE 自检均 exit 0：

| 自检 | 源码自检耗时 | EXE 自检耗时 | 结果 |
| --- | ---: | ---: | --- |
| credentials | 0.77s | 0.69s | exit 0 |
| database | 1.53s | 1.33s | exit 0 |
| resources | 0.64s | 0.81s | exit 0 |
| report-export | 0.82s | 0.90s | exit 0 |

构建产物：

| 产物 | 大小 | SHA-256/树哈希 |
| --- | ---: | --- |
| `dist/PaperReviewer/PaperReviewer.exe` | 16,950,744 bytes（16.17 MiB） | `32ce9785e3d9840e50c463b9b9f68560bcaa7cfbd1507ddc32457fbd73974970` |
| `dist/PaperReviewer-portable.zip` | 95,190,607 bytes（90.78 MiB） | `46f3dbc5172b4bfc73d26ab4417802780c79ce2244d6b57a37b1157859841227` |
| `dist/PaperReviewer/` onedir | 404 files，204,871,626 bytes（195.38 MiB） | `4e1604c060cd879b6df666c928ba2184e66a0f43e7c3c04649c2fcd3e067f8d3` |

PyInstaller 报告未找到 `tzdata`、`pysqlite2`、`MySQLdb`、`psycopg2`；四项自检通过，运行使用 SQLite/aiosqlite。未安装 Python 的 Windows 环境 EXE GUI 冒烟未执行。

## 7. 重构指标对比

| 指标 | 重构前基线 | 重构后 | 备注 |
| --- | ---: | ---: | --- |
| `run_detail.py` 行数 | 1862 | 1500 | 最终源码提交工作树统计 |
| `orchestrator.py` 行数 | 1539 | 1366 | 最终源码提交工作树统计 |
| `service.py` 行数 | 1410 | 1342 | 最终源码提交工作树统计 |
| Ruff 复杂度热点 | execute 约 42；run_bounded_agent 约 45 | `execute` = 7（77 行）；`run_bounded_agent` = 1（29 行） | Ruff C901 审计 |
| dist/PaperReviewer 目录 | 约 195.35 MiB | 404 files，195.38 MiB | 树哈希见第 6 节；本轮不以缩容为目标 |
| 便携 ZIP | 约 95 MB | 95,190,607 bytes（90.78 MiB） | SHA-256 见第 6 节 |
| GUI 冷启动自检 | 约 2.17–2.66 秒 | offscreen N=9：进程至 MainWindow 构造中位数 1.5165s | 不含 show/首帧，不能称可见首帧性能 |

## 8. 数据、兼容性与安全检查

- 本次未增加数据库迁移；自动化回归通过。
- 恢复前新增 Provider 快照与 RunRecord 的 provider/model 一致性校验；新增安全回归通过，防止恢复时使用不匹配的模型快照。
- 交付文档扫描未发现 API Key、Bearer/Authorization 值、论文正文或个人绝对路径；运行错误和 traceback 仍需按敏感材料人工复核。
- 构建目录、临时目录、数据库和本地凭据未纳入本次文档交付修改。
- 相对 Markdown 链接全部存在、无尾随空白，架构/Provider 文档 `git diff --check` 通过。

## 9. 未执行项目与剩余风险

1. 未安装 Python 的独立 Windows 环境中，启动 EXE GUI、配置 Provider、载入 Rubric、创建/取消/恢复任务并打开报告：未执行；本次已完成 EXE 四项自检。
2. Chat/Responses/自定义 Provider：模拟/fixture 自动化回归通过；真实付费 Provider 请求及其费用/服务商兼容矩阵未执行。
3. 长中文多页 PDF：自动导出自检通过；逐页 PNG 版式与人工文本抽取检查未执行。
4. 取消、崩溃恢复、旧任务和旧 Schema v1 组合测试：相关自动化回归通过；独立端到端人工组合测试未执行。
5. 100%–200% 显示缩放、高对比度、NVDA 和完整可访问性检查：未执行。
6. 最终构建目录、ZIP 和哈希校验：已执行；产物大小、SHA-256 和 onedir 树哈希见第 6 节。

剩余风险是人工复核、真实 Provider/网络环境和特定 Windows GUI 配置尚未覆盖。缓解措施是保留四项 EXE 自检、限定发布为有条件通过，并在真实部署前完成上述检查。无已知高风险自动化失败。

## 10. 发布判定

    构建源码提交：eafd4482e239a4513821e4821da4c329f8fbdea4
    测试日期：2026-08-26
    自动化检查：Ruff src/tests/migrations exit 0；mypy src 的 101 files 无问题；pytest 283 passed in 56.20s
    打包自检：构建 wall time 约 119s；credentials/database/resources/report-export 的源码与 EXE 自检均 exit 0
    EXE 冒烟：四项自检通过；未安装 Python 的 Windows GUI 冒烟未执行
    未执行项目：第 9 节所列人工 GUI、真实 Provider、PDF 逐页和特定环境检查
    高风险失败：无已知高风险自动化失败；人工/外部检查仍有残余风险
    发布判定：有条件通过
    判定理由：自动化回归、源码自检、EXE 自检和产物哈希均已完成，但尚未完成无 Python Windows GUI 冒烟、真实付费 Provider 请求、人工可访问性及代表性 PDF 版式检查。

本判定不替代部署前的人工和环境验收，也不改变项目关于人工复核、涉密材料和正式抽检用途的限制。
