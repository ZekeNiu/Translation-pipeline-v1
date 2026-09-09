# 实现范围与后续取舍

本轮以英文论文、专业书籍的完整性和可恢复性为主，保留桌面界面、MinerU 与 Markdown / DOCX 输出。

## 已实现

- Windows 用户配置与 DPAPI Key 保存；厂商设置互不覆盖；输入模式按需显示，日志和高级设置折叠。
- 官网 v4 协议、按实际页数和体积分片、章节边界优先、接缝补充解析、分片状态恢复和同名图片隔离。
- 正文结构块、完整 HTML 表格、原位置参考文献、相邻原文上下文、段落与占位符校验。
- 只修订有疑点的段落 / 单元格；合格结果不会因修订失败被替换。
- 来源及配置指纹、结果哈希、原子保存、停止并继续、完成 / 待检查 / 部分失败三种状态。
- 分阶段耗时和重试请求耗时、连接复用、已校验请求缓存和同文档表格文本复用。默认保持两路并发。

模块按职责分开：`settings_store.py` 保存设置；`pdf_parts.py` 分片；`mineru_cloud.py` 管理官网任务；`mineru_merge.py` 合并结果；`document_blocks.py` 与 `translation_checks.py` 定义结构和检查；`translation_job.py` 编排续跑。`mineru_runner.py` 和 `translate.py` 保留返回目录 / 输出路径的接口。

## 借鉴依据

- [deusyu/translate-book](https://github.com/deusyu/translate-book)：借鉴来源指纹、分段检查、断点恢复和短原文上下文的思路。
- [KazKozDev/book-translator](https://github.com/KazKozDev/book-translator)：借鉴只修订具体问题、保留合格结果的思路。

实现独立编写，未复制这两个项目的源码或引入其多模型工作流；并未把项目知名度视为翻译质量证据。

## 后续取舍

本轮不增加全书二次审校、自动 Office 转换、新导出格式或复杂任务管理页面。后续应先用真实长书积累接缝与翻译质量样本，再决定是否需要人工术语表、定向重新翻译和额外格式支持。

接缝修复采用保守映射，无法确认的 OCR 差异保留并报告。扫描内容的实际识别质量仍取决于解析服务。Word 保留阅读结构，不复刻原始 PDF 版面。详见[验收记录](validation-and-rollback.md)。
