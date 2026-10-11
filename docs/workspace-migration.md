# 旧工作台内容迁移

`tools/migrate_workspace.py` 只读旧 SQLite，将选定业务资料复制为 Obsidian 可读 Markdown。原数据库、原文件和旧会话不移动、不删除、不覆盖。

```sh
python3 tools/migrate_workspace.py --data-dir /absolute/old-data --target-dir /absolute/vault --dry-run
python3 tools/migrate_workspace.py --data-dir /absolute/old-data --target-dir /absolute/vault
```

程序接口：`migrate(data_dir, target_dir, dry_run=False, include_sessions=False)`，返回 `created / skipped / conflicts / errors` 明细及 `counts`。dry-run 只计算计划，不创建目录或文件。

默认范围：

- objects.kind=note：保留原标题、正文、类型、来源、关联目标与复盘期。
- objects.kind=goal：保留原状态、原因、日期、优先级、行动、完成标准、实际结果；草稿与候选不因迁移变成已采纳事项。若原记录里 draft 与 done 标记矛盾，元数据保留原标记，正文标注候选尚未采纳，任务框不替它认定完成。
- objects.kind=learning：保留顾问、资料 ID、学习状态、进度与实际心得。
- planning/*.md：保留正文，附原相对路径与内容哈希；明确方案未自动采用。
- 首页.md：聚合本次成功复制或内容一致的笔记链接、解释目录用途。

**默认不复制** job、runs、模型 prompt、raw_response 或会话全文。`--include-sessions` 仅额外生成会话标题、日期、消息数量与旧 ID 的索引，仍不复制消息正文；默认不开启。

文件初次命名为 `标题短名--原ID.md`，元数据保留完整 legacy_id/revision。重跑根据完整 ID 查找已有位置，所以来源改名不会复制第二份。内容完全一致为 skipped；已有文件不同为 conflict，必须人工处理，不会覆盖用户在 Obsidian 或向上里写的新内容。首页同样不覆盖。

目录逐级以 no-follow 描述符打开，新文件采用排他创建，回读内容确认；拒绝符号链接、路径越界、异常 ID。目标不能是原数据目录或其 planning 子树，以免迁移递归复制自己。

验收使用独立临时 SQLite 和文件夹，15 项测试通过：正常迁移、状态保真、重跑幂等、改名稳定、用户修改冲突、首页保留、隐私执行日志排除、会话可选索引、dry-run 无写入、原数据库及方案不变、符号链接与路径穿越拒绝。
