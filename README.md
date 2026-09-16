# 学术文档翻译

将英文论文、专业书籍翻译为中文，输出 Markdown 和 Word。保留 MinerU 解析正文、图片和结构信息，优先检查完整性，并保存长任务进度。

## 开始使用

Windows，Python 3.11 或更高版本：

```powershell
python -m pip install -r requirements.txt
python gui.py
```

也可以双击 `run_gui.bat`。依次选择材料、确认翻译服务、点击“开始 / 继续任务”。

| 输入方式 | 使用方法 |
| --- | --- |
| 已有 MinerU 文件夹 | 选择包含 `full.md` 和图片的解析目录。 |
| MinerU API | 选择文件；官网 URL 默认 `https://mineru.net`，填写官网 Token。 |
| 本地 MinerU CLI | 选择文件，使用本机 `mineru`；程序路径和后端放在高级设置。 |

支持 DeepSeek、OpenAI、Qwen、Gemini 的兼容接口、SiliconFlow 和自定义 Chat Completions 服务。模型可以直接填写，“刷新模型”只在点击时联网。

URL、输入方式、模型和各厂商的独立设置自动保存，重启恢复。设置位于 `%LOCALAPPDATA%\TranslationPipeline\settings.json`；Key 由 Windows 当前账户的 DPAPI 加密。清空 Key 会保存为空。首次启动从现有 `.env` / 环境配置读取；之后优先恢复界面保存值。保存失败会显示提示，不丢弃当前输入。

高级设置包含输出目录、本地解析后端和并发模式，日志默认折叠。默认两路翻译并发；开始后本次任务配置固定，继续任务时使用界面中的配置。

## 长 PDF 与官网解析

官网接入按 [MinerU 官方 API 文档](https://mineru.net/apiManage/docs)实现上传地址申请、上传、批次查询与结果下载，使用 `vlm` 并开启公式、表格识别。每批最多 50 个文件。保留自建服务的 `/file_parse` 和 `/tasks` 兼容入口；只有端点明确不存在才切换协议。

PDF 每个上传部分最多 200 页、190,000,000 字节。优先采用书签章节、小节和段落附近的页边界，实际落盘测量体积，复制原始页面而不降低图像清晰度。主分片页码不重叠且覆盖完整。缺乏结构线索的接缝补充解析前后页，只有页码与内容能唯一对应时才修复；不确定的接缝保留主分片内容，在报告中列出页码。

解析结果默认保存在 `mineru_outputs/`，包括原始结果、分片和状态。已完成部分通过哈希验证后复用；中断后先查询已提交的官网任务。API Token 不发送给对象存储上传、下载地址。

超限 Office 文件请先导出为 PDF。受密码保护而不可读、损坏或单页仍超限的 PDF 会在上传前停止并说明原因。自建服务保留原有协议，其断点能力取决于服务端；官网分片与续跑流程不适用于该兼容入口。

### 本地 MinerU CLI

本地模式调用 `mineru.exe`，程序路径应填写可执行文件的完整路径。留空时依次检查系统 PATH 和当前用户登记的 Conda 环境；如果安装了多个版本，请明确指定需要使用的那一个。

项目会读取所选 Conda 环境的 `conda-meta/state`，将其中保存的模型配置与环境路径传给 MinerU 子进程。点击“高级设置 → 检测环境”可查看实际程序路径和版本；检测失败时会显示具体原因。`auto` 使用该 MinerU 版本的默认后端。

本地后端选项：`auto`、`hybrid-engine`（混合解析）、`vlm-engine`（视觉语言模型）、`pipeline`（传统解析）。名称以本机验证的 MinerU 3.4.5 为准，该版本默认 `hybrid-engine`；旧版本可继续选择 `auto`。后端是解析方法，不是 CPU/GPU 开关：本地环境支持 CUDA 时可以使用 GPU，`pipeline` 也不代表只用 CPU。实际设备以解析日志为准，例如 `lmdeploy device is: cuda`。`vlm-http-client` 和 `hybrid-http-client` 需要另配推理服务器地址，因此没有列入本地直接运行的选项。

解析时界面持续显示当前阶段、耗时和最近活动；只有 MinerU 提供可靠页数时才显示页数进度。停止会结束当前任务启动的进程树。原始日志保存在每次解析的 `attempt_*/cli_stdout.txt`、`cli_stderr.txt`。

本地解析复用同时检查源内容、程序版本、后端、模型配置、模型文件清单及结果文件。模型文件清单比较路径、大小和修改时间，不完整读取权重文件。远程模型来源或缺失配置无法确认版本时，会说明原因并重新解析。旧缓存保留，可用“已有 MinerU 文件夹”主动选择。

## 专业术语表

先选择材料，再点击“专业术语表”。全局通用和本书专用词库分别开关，默认关闭，按书记住选择；同一原词以本书译法优先。切换模型或移动原文件不改变本书关联。

- 支持搜索、新建、编辑、删除及 CSV 导入导出。CSV 使用 UTF-8，包含 `source,target,note` 三列，`note` 可省略；同层冲突在导入时逐条选择。
- “提取本书术语建议”会在需要时先解析原材料，再主动调用当前翻译服务。最多发送 100 个本地筛选候选及短上下文；仅用户选中的建议加入本书词库，之后仍可编辑。
- 词库保存在当前用户的 `TranslationPipeline/glossaries/`，不上传 GitHub。每次运行冻结一份快照；运行中的编辑从下一次任务生效。
- 每个请求只附带命中的原词和译法，备注不发送。关闭或未命中时不增加术语提示；增加无关词条不重译已完成内容。修改译法只使相关分段或单元格缓存失效。
- 术语可能增加输入用量；报告记录附加字符、修订请求和服务返回的用量，不估算未经配置的金额。术语提取仅在点击按钮时发生。

CLI 仅启用明确传入的 CSV，不自动读取 GUI 词库：

```powershell
python translate.py "C:\MinerU\output_folder" --global-glossary "C:\terms\common.csv" --book-glossary "C:\terms\book.csv"
```

## 双语复核

完成或继续任务后点击“复核译文”。独立窗口显示原文、译文、章节及检查项，可筛选待检查内容。表格按单元格复核；公式、图片、代码与参考文献保持结构保护。

- 手动编辑后点击“保存并重新导出”，更新 Markdown、DOCX 和质量报告；保存会检查数字及受保护结构，普通疑点可以明确保留，结构损坏不能覆盖。
- “只重译选中项”使用原任务的服务与模型、当前本书术语设置，生成候选供采用；不会自动替换整书。当前窗口须有原服务可用的 Key。
- “恢复上一版”保留历史；人工修订独立保存，续跑优先使用。术语约束变化后保留人工译文并标记待复核。
- Word 文件被占用等原因导致导出失败时，修订仍已保存；点击“重新导出”不再调用模型。操作前版本在 `_review_history/`。
- 有可靠页码及原 PDF 时，“查看原 PDF 页”提取对应原页并用默认阅读器打开；未定位或没有原 PDF 时仍可对照文本，不猜测页码。
- 旧任务可点击“离线恢复对应记录”，须选择原任务的服务、模型和参数。只使用已校验缓存；缓存不足的内容保持只读，不自动调用模型补齐。

翻译进行中复核暂为只读；不同窗口同时修改同一任务会被锁阻止。任务升级前保留旧记录；原译对应保存在 `review_document.json`，人工修改保存在 `review_edits.json`，局部重译用量在 `review_requests.json`。

## 完成、检查与继续

点击“停止并保留进度”，当前网络请求结束后停止。重新选择同一材料和配置，再点击“开始 / 继续任务”。通过校验的部分可复用，失败部分会重试。替换源文件，或改变模型、服务 URL、温度、提示词和上下文后，不复用不匹配的译文。切换 Key 不会使合格译文失效。

结果默认位于 `translations/标题_任务指纹/`。点击“打开结果”：

| 文件 | 用途 |
| --- | --- |
| `translated.md`、`translated.docx` | 译文及可随目录移动的图片。 |
| `quality_report.md` | 需要检查的段落、表格、图片和接缝页码。 |
| `quality_report.json`、`translation.log` | 详细状态、耗时和请求用量。 |
| `task_state.json`、`_chunks/`、`_cache/` | 续跑记录与已校验结果，请保留。 |
| `references_original.md`、`references_normalized.md` | 检测到参考文献时生成的对照文件。 |

“完成”表示自动检查未发现问题；“完成，但有待检查项”需要查看报告；“部分失败”中未完成材料保留原文，可继续任务。自动检查不能代替学术语义审校。

完整 HTML 表格按单元格翻译，保留行列和合并关系；公式、图片、代码及参考文献受结构标记保护。参考文献保留原位置，后续同级章节与附录继续翻译。正文使用章节标题和相邻原文短摘录作为上下文，疑点修订只发送相关段落或单元格。Word 公式沿用现有的可读文本与上下标导出，并非原 PDF 版式或完整的 Word 公式编辑器对象。

## 命令行与开发

CLI 沿用 `.env` / 环境变量和命令参数规则，不读取 GUI 保存的 Key。可复制 `.env.example` 为 `.env` 后配置需要的服务。

```powershell
python translate.py "C:\MinerU\output_folder"
python translate.py "C:\MinerU\output_folder" --provider qwen --model qwen-plus --speed-mode balanced
python -m unittest discover -s tests -v
```

`safe` / `balanced` / `fast` 分别最多使用 1 / 2 / 4 路翻译并发，检查规则相同。生成学术测试样例：

```powershell
python -m pip install -r requirements-dev.txt
python tests/manual_smoke.py
```

该命令默认离线；显式添加 `--live --output tests/.artifacts/live` 才会使用已配置的翻译服务。样例和运行产物不进入 Git。

[验收记录与回退方法](docs/validation-and-rollback.md) · [解析后停留在 0/N 的修复与实测](docs/bugfix-20260909.md) · [实现范围与后续取舍](docs/long_book_translation_roadmap.md)

[术语、复核及本地解析升级验收与回退](docs/high-priority-20260915.md)
