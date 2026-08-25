# 论文评测项目交付索引

> 本索引是本项目交付材料的统一入口。构建源码提交、构建和测试结果已回填；发布判定仍受验收报告中未执行项目的限制。

## 1. 项目身份

| 项目 | 内容 |
| --- | --- |
| 项目名称 | Paper Reviewer（论文评测） |
| 当前工程版本 | `0.1.0`（以最终 `pyproject.toml` 为准） |
| 交付定位 | 面向教师和高校质量控制人员的 AI 辅助论文预评与抽检风险评议工具 |
| 支持平台 | Windows 10/11 便携版；开发环境要求以 `pyproject.toml` 为准 |
| Git 分支 | `main` |
| 构建源码 Git Commit | `eafd4482e239a4513821e4821da4c329f8fbdea4` |
| 交付日期 | 2026-08-26 |
| 规则来源 | 《浙江省本科毕业论文（设计）抽检实施细则（试行）》及当前 Rubric 快照 |

本项目不产生浙江省教育厅正式抽检结论，也不应直接用于自动处分、学位决定或正式抽检认定。百分制和五级锚点是项目自定义的实验性诊断规则。

## 2. 推荐阅读顺序

1. [USER_GUIDE.md](USER_GUIDE.md)：普通用户的安装、配置、评测、恢复、人工复核和报告导出说明。
2. [TROUBLESHOOTING.md](TROUBLESHOOTING.md)：常见界面提示、API、PDF、Rubric、恢复和导出问题的处理方法。
3. [PROJECT_REPORT.md](PROJECT_REPORT.md)：项目目标、评分模型、Agent/Harness 架构、数据流、安全边界和限制。
4. [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)：后续开发、调试、扩展 Rubric/Provider 和 GUI 的约束。
5. [BUILD_AND_RELEASE.md](BUILD_AND_RELEASE.md)：PyInstaller 构建、便携 ZIP、四项自检和发布检查。
6. [TEST_ACCEPTANCE_REPORT.md](TEST_ACCEPTANCE_REPORT.md)：最终环境、命令、通过数量、未执行项目和发布结论。
7. [RELEASE_NOTES.md](RELEASE_NOTES.md)：版本变化、兼容性和升级注意事项。

## 3. 交付件清单

### 源码和配置

| 交付件 | 位置 | 状态 |
| --- | --- | --- |
| Python 源码 | `src/paper_reviewer/` | 随最终 Git 提交交付 |
| Rubric | `configs/rubrics/` | 随最终 Git 提交交付 |
| Reviewer Profile | `configs/review_profiles/` | 随最终 Git 提交交付 |
| 数据库迁移 | `migrations/` | 随最终 Git 提交交付 |
| PyInstaller 规格 | `paper-reviewer.spec` | 随最终 Git 提交交付 |
| 构建脚本 | `scripts/build_portable.ps1` | 随最终 Git 提交交付 |

### 桌面程序

| 交付件 | 预期位置 | SHA-256 | 大小 | 状态 |
| --- | --- | --- | --- | --- |
| 便携目录 | `dist/PaperReviewer/` | `4e1604c060cd879b6df666c928ba2184e66a0f43e7c3c04649c2fcd3e067f8d3`（树哈希） | 404 files，204,871,626 bytes（195.38 MiB） | 构建成功 |
| Windows 可执行文件 | `dist/PaperReviewer/PaperReviewer.exe` | `32ce9785e3d9840e50c463b9b9f68560bcaa7cfbd1507ddc32457fbd73974970` | 16,950,744 bytes（16.17 MiB） | 构建成功，EXE 四项自检 exit 0 |
| 便携 ZIP | `dist/PaperReviewer-portable.zip` | `46f3dbc5172b4bfc73d26ab4417802780c79ce2244d6b57a37b1157859841227` | 95,190,607 bytes（90.78 MiB） | 构建成功 |

便携版目标计算机不需要安装 Python。发布时应整体分发 onedir 目录或 ZIP，不要单独拷贝 EXE。

### 文档

| 文档 | 用途 |
| --- | --- |
| [PROJECT_REPORT.md](PROJECT_REPORT.md) | 技术项目报告与架构说明 |
| [USER_GUIDE.md](USER_GUIDE.md) | 用户操作说明 |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 常见问题与报错对应 |
| [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) | 开发、扩展和维护说明 |
| [BUILD_AND_RELEASE.md](BUILD_AND_RELEASE.md) | 构建、打包和发布说明 |
| [TEST_ACCEPTANCE_REPORT.md](TEST_ACCEPTANCE_REPORT.md) | 实际验收记录 |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | 本版本发布说明 |

以上文档均为本次交付的一部分；最终验收数值以 `TEST_ACCEPTANCE_REPORT.md` 为准。

## 4. 关键运行数据位置

桌面端通过系统目录解析器将运行数据放在：

```text
%LOCALAPPDATA%\PaperReviewer\PaperReviewer\
├─ data\       SQLite 数据库
├─ runs\       每个任务的论文快照、检查点、证据和报告
├─ logs\       应用日志
└─ config\     非秘密偏好和自定义 Provider 目录
```

API Key 不写入上述 JSON、Trace、数据库或报告；Windows 桌面端优先使用 Windows Credential Manager，内置 Provider 缺少凭据时可从进程环境变量回退，自定义 Provider 不使用环境变量回退。自定义 Provider 的非秘密目录配置与任务快照不含 API Key。

## 5. 最终验收记录

以下内容来自 2026-08-26 的最终验收终端记录：

| 项目 | 实际结果 |
| --- | --- |
| 操作系统和架构 | Windows 11 build 26200；架构未单独记录 |
| Python / PySide6 / PyInstaller | Python 3.12.13；PySide6 6.11.2；PyInstaller 6.22.2 |
| Ruff | `ruff check src tests migrations`，exit 0 |
| mypy | `mypy src`，101 files 无问题，exit 0 |
| pytest | 283 passed，0 failed，exit 0，56.20s |
| GUI、数据库、凭据、资源自检 | 源码与 EXE 四项自检均 exit 0；EXE 0.69/1.33/0.81/0.90s |
| Markdown/PDF 导出自检 | EXE 自检完成确定性 Markdown→PDF 渲染及 PyMuPDF 重开；快照重建与 Markdown 字节导出由 pytest 覆盖；代表性 PDF 逐页人工版式检查未执行 |
| Chat、Responses、自定义 Provider 兼容服务冒烟 | 模拟/fixture 自动化回归通过；真实付费 Provider 请求未执行 |
| 未安装 Python 的 EXE 冒烟 | 未执行；仅完成 EXE 自检 |
| 文档链接和脱敏检查 | 相对链接、尾随空白、个人路径、Key/Bearer 扫描通过 |
| 是否满足发布条件 | 有条件通过；限制和未执行项目见 TEST_ACCEPTANCE_REPORT.md 第 9 节 |

## 6. 已知范围与限制摘要

- 首版以普通本科论文 PDF 为主；扫描件、设计作品、图纸、涉密论文和特殊培养成果不在默认支持范围内。
- 外部检索是可选的；服务异常会降级为证据或审计提示，不代表论文结论被验证。
- “添加查重/学术不端检测报告”目前只保留入口，不选择、解析、保存或上传检测报告；教师可在线下核查后在人工复核理由中记录结论。
- AI 可以提出政治方向或学术诚信嫌疑，但不能自动确认；待办完成前风险结论保持待定。
- Rubric 标注为实验性且尚未完成教育测量效度验证；需要人工复核和后续金标准校准。
