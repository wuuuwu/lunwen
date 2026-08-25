# 发布说明

## 版本信息

| 项目 | 值 |
| --- | --- |
| 项目 | Paper Reviewer 论文评测 |
| 版本 | 0.1.0（当前项目版本） |
| 基线提交 | 1bee1ac |
| 构建源码提交 | eafd4482e239a4513821e4821da4c329f8fbdea4 |
| 发布日期 | 2026-08-26 |

本次版本以“等价重构与交付文档完善”为主题。构建产物、哈希和验收结论已按最终安全修复提交回填；发布判定为有条件通过。

## 本次重构范围

- 拆分评测流水线、Agent 循环、应用服务和大型 GUI 页面，降低单个函数和页面的职责复杂度。
- 收敛事件描述、任务 Artifact 写入、运行时资源管理、报告投影和部分持久化映射逻辑。
- 清理经过引用搜索和测试确认的冗余代码、无效导入及未使用开发依赖。
- 恢复任务前校验任务记录与 Provider 快照的 `provider` 和 `model` 一致性，拒绝不匹配的模型快照，新增安全回归用例已通过。
- 保留 Qt Widgets、Fluent 主题、Windows 原生标题栏和现有页面布局。
- 补充项目验收、用户使用、开发维护和常见报错的交付文档入口。

## 明确保留的功能行为

以下行为是本次重构的兼容约束，不应因为代码拆分而改变：

- GUI、CLI 和评分流程的用户可见功能。
- LLM 调用次数、预算、并发方式、重试策略和工具调用协议。
- OpenAI Chat Completions、OpenAI Responses、DeepSeek 及自定义 Provider 的协议边界。
- Schema v1/v2、Rubric 快照、旧任务、历史报告和数据库迁移兼容。
- 3+2 专家面板、否决项、评测完成后人工复核和本地确定性刷新。
- 任务状态、事件顺序、取消、检查点保存和中断恢复语义。
- Markdown 基准报告、PDF 导出、中文展示、免责声明和审计字段。
- 桌面端优先把 API Key 存放在 Windows Credential Manager；内置 Provider 仍可从进程环境变量兼容读取，且 Key 不写入 Provider JSON、任务快照、Trace、数据库或报告。兼容性诊断使用白名单字段，其他运行错误和 traceback 仍需人工脱敏检查。

本文件不代替自动化验收。功能是否确实保持不变，应以 TEST_ACCEPTANCE_REPORT.md 的实际结果为准。

## 兼容性与数据迁移

- 本次重构不增加数据库迁移。
- 不要求用户迁移数据库、历史任务、Rubric 或报告。
- 旧任务缺少新快照时继续按照现有兼容回退规则处理；旧 OpenAI/DeepSeek 任务的历史语义不改变。
- 任务目录中的 Artifact 文件名、JSON 形状和文件/数据库优先级属于兼容边界。
- 现有 %LOCALAPPDATA%\PaperReviewer\PaperReviewer 数据目录和 Windows 凭据不应被清理或覆盖。
- 升级前建议备份数据库、任务目录和自定义 Rubric；不要备份或导出明文 API Key。

## 已知限制

- 系统是教师和高校质控人员使用的 AI 辅助预评工具，不是浙江省教育厅正式抽检工具。
- 九项百分制和五级锚点是项目自定义诊断规则，尚未完成教育测量效度验证。
- 结果不得直接用于自动处分、学位决定或正式抽检认定。
- 云端评测前必须确认处理授权并确认论文不含涉密材料。
- 查重/学术不端检测报告入口仍是预留按钮；首版不选择、解析、上传或保存该文件。
- 外部学术检索依赖网络和公共服务；搜索失败会产生警告或降级，不代表论文结论。
- PDF 必须能够由当前解析器抽取可用文本；纯扫描件、加密 PDF、损坏 PDF 或复杂版式可能无法评测。
- Responses API 的模型、工具调用能力和服务商兼容性由服务商端决定；不支持工具调用的端点不能作为完整 Agent Provider。
- 兼容性测试可能产生真实请求和费用；测试失败不等同于配置保存失败，具体原因应查看脱敏诊断。
- 不承诺固定的 EXE 体积下降；本轮不裁剪 Qt、DDGS 或 keyring 的 PyInstaller 收集范围。

## 报错与支持入口

用户遇到问题时，优先查看 TROUBLESHOOTING.md，并提供：

- 界面显示的脱敏错误信息。
- Provider 协议、模型名称和 HTTP 状态（不要提供 API Key）。
- 任务 ID、阶段和状态。
- 是否可以恢复、是否保留检查点。
- logs 目录中的相关时间段日志（先确认已删除密钥、请求头和论文正文）。

不要提交 API Key、Bearer Header、完整论文、查重报告、服务商原始响应或包含敏感 URL 的截图。

## 构建与发布状态

目标发布方式仍为 PyInstaller onedir 便携目录，并压缩为 ZIP。目标文件名：

- dist\PaperReviewer\PaperReviewer.exe
- dist\PaperReviewer-portable.zip

最终构建成功，工具 wall time 约 119s。源码与 EXE 的 credentials/database/resources/report-export 四项自检均 exit 0；EXE 自检耗时分别为 0.69s、1.33s、0.81s、0.90s。未安装 Python 的 Windows GUI 冒烟未执行，发布地址未设置。

最终产物：

| 产物 | 大小 | SHA-256/树哈希 |
| --- | ---: | --- |
| `dist/PaperReviewer/PaperReviewer.exe` | 16,950,744 bytes（16.17 MiB） | `32ce9785e3d9840e50c463b9b9f68560bcaa7cfbd1507ddc32457fbd73974970` |
| `dist/PaperReviewer-portable.zip` | 95,190,607 bytes（90.78 MiB） | `46f3dbc5172b4bfc73d26ab4417802780c79ce2244d6b57a37b1157859841227` |
| `dist/PaperReviewer/` onedir | 404 files，204,871,626 bytes（195.38 MiB） | `4e1604c060cd879b6df666c928ba2184e66a0f43e7c3c04649c2fcd3e067f8d3` |

## 未纳入本次版本的行为修复

本次重构不顺带改变以下范围：

- 评分规则、Rubric 教育学效度和模型质量校准。
- 新增论文类型、设计作品、图纸或涉密论文支持。
- 查重报告文件的解析、持久化和模型读取。
- CLI 中自定义 Provider/Responses 入口的扩展范围。
- 数据库结构重设计、后台托盘运行和自动报告生成。
- PyInstaller 包体积专项优化。
- 任何需要改变现有错误提示、协议参数、模型调用次数或报告历史字节内容的独立修复。

这些事项应作为单独需求评审，不能以“重构”名义隐式加入本版本。

## 升级与回滚建议

升级前关闭正在运行的评测，备份 %LOCALAPPDATA%\PaperReviewer\PaperReviewer 下的数据库和任务 Artifact。保留旧便携目录，先在副本上完成启动、资源、数据库和报告导出自检。若最终验收失败，恢复旧 EXE 目录即可；不要使用 git reset --hard 或删除用户数据来处理发布问题。

## 发布确认

    构建源码提交：eafd4482e239a4513821e4821da4c329f8fbdea4
    构建日期：2026-08-26
    EXE/ZIP：EXE 16,950,744 bytes；ZIP 95,190,607 bytes；onedir 404 files / 204,871,626 bytes
    SHA-256：EXE 32ce9785e3d9840e50c463b9b9f68560bcaa7cfbd1507ddc32457fbd73974970；ZIP 46f3dbc5172b4bfc73d26ab4417802780c79ce2244d6b57a37b1157859841227
    验收报告：TEST_ACCEPTANCE_REPORT.md；283 passed in 56.20s；四项 EXE 自检 exit 0
    发布判定：有条件通过；无 Python Windows GUI 冒烟、真实付费 Provider 请求、人工可访问性和代表性 PDF 逐页版式检查
