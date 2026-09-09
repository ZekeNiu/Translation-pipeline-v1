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
