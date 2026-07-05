# 翻译流水线 v2.1

将 MinerU 解析出的 Markdown 自动翻译为中文，尽量保留论文/书籍中的标题、公式、图片、表格和参考文献结构。

## 文件结构

```text
D:\AI\Translation-pipeline-v1\
├── translate.py        # 核心翻译流水线
├── table_utils.py      # HTML 表格解析/渲染工具
├── latex_utils.py      # LaTeX 公式可读化
├── inline_semantics.py # inline 公式/上标/HTML 语义保护
├── mineru_sidecar.py   # MinerU JSON 结构辅助读取
├── mineru_runner.py    # 可选调用 MinerU CLI/API 的输入适配层
├── make_docx.py        # DOCX 生成器
├── gui.py              # 图形界面
├── run.bat             # CLI 启动
├── run_gui.bat         # GUI 启动
├── .env.example        # 配置模板
└── requirements.txt    # 依赖包
```

## 安装

```bash
pip install -r requirements.txt
```

## 配置 AI 厂商

支持 OpenAI-compatible Chat Completions 接口。内置预设包括 DeepSeek、OpenAI、Qwen/DashScope、Gemini OpenAI-compatible、SiliconFlow 和自定义接口。

复制 `.env.example` 为 `.env` 后填写需要的 Key：

```env
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-your-key
DEEPSEEK_MODEL=deepseek-v4-flash
```

也可以在 GUI 里直接选择厂商并填写 Base URL、模型名和 API Key。

## 运行

拖拽 MinerU 输出文件夹到 `run.bat`，或使用命令行：

```bash
python translate.py "C:\MinerU\output_folder"
```

指定厂商和模型：

```bash
python translate.py "C:\MinerU\output_folder" --provider qwen --model qwen-plus
```

速度模式：

```bash
python translate.py "C:\MinerU\output_folder" --speed-mode balanced
```

`safe` 为 1 路串行，`balanced` 默认 2 路并发，`fast` 最多 4 路并发。长书建议先用 `balanced`。

自定义 OpenAI-compatible 接口：

```bash
python translate.py "C:\MinerU\output_folder" --provider custom --base-url "https://example.com/v1" --model "your-model"
```

GUI：

```bash
run_gui.bat
```

GUI 支持三种输入来源：

- `已有 MinerU 文件夹`：选择已经由 MinerU 生成的输出目录，最稳妥。
- `本地 MinerU CLI`：选择 PDF/图片/Office 文件，调用本机 `mineru` 命令解析，再自动进入翻译。
- `MinerU API`：填写 MinerU API Base URL/API Key，上传文件解析，再自动进入翻译。

本地 MinerU 默认使用新版命令：

```bash
mineru -p <input_path> -o <output_path>
```

CPU/pipeline 模式会追加：

```bash
mineru -p <input_path> -o <output_path> -b pipeline
```

如果 `mineru` 不在 PATH 中，可以在 GUI 的 `MinerU CLI` 中填写可执行文件路径。MinerU 中间输出默认写入 `mineru_outputs\`，该目录不会上传到 Git。

## 输出

输出默认写入项目下的 `translations\时间_标题\`，包含：

- `translated.md`
- `translated.docx`
- `translation.log`
- `references_original.md`（如果检测到参考文献）
- `references_normalized.md`（如果检测到参考文献）
- `_cache\`（chunk 级缓存，用于同一输出目录下断点续跑）

## 当前质量策略

- Markdown 仍是主输入；如果 MinerU 文件夹中存在 `*_content_list_v2.json`、`layout.json`、`block_list.json`，会自动作为结构辅助读取。
- JSON sidecar 用于识别公式、页眉页脚、页码、表格/图片/caption 类型；不会替代 Markdown 正文。
- 页眉、页脚、页码默认不写入译文正文，DOCX 不复刻原 PDF 页眉页脚。
- 公式、图片、HTML 表格在正文翻译时使用结构化占位符保护；混合文本和上标脚注会先拆成可翻译文本和结构标记。
- DOCX 导出时会把常见 MinerU LaTeX OCR 公式转为可读文本，例如 `\mathrm { O } _ { 2 }` → `O₂`。
- HTML 表格单元格会单独翻译，并在 DOCX 中生成真正的 Word 表格。
- 参考文献会提前隔离并归并 OCR 断开的续行，避免被正文翻译改写。
- 不再全局删除 HTML 标签、美元符号或大括号，避免破坏 `<45`、`\frac{a}{b}` 等内容。
- 文档内会自动生成轻量一致性约束：缩写按通用规则保留，参考文献/明确姓名机构不翻译，自动术语候选只用于日志审计。
- GUI 中“刷新模型”只在手动点击时请求当前厂商的 `/models` 接口；失败时仍可手动填写模型名。
- 表格翻译失败会自动拆成更小批次重试，无法翻译的单元格会记录到日志，不再整表静默回退。
