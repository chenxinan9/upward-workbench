# 本地接入接口

这是可替换的本地文件协议，不捆绑某位作者的私人路径、检索数据库或资料库。

私人数据目录中的 `settings.json` 可包含 `codex_cli`、`catalog`、`advisor_config`、`advisor_script`。这些字段都是本机可信配置，不接受网页远程设置。`focus` 是带 `title`、`detail`、`date` 的历史方向参考列表，仅展示、不创建活跃目标。日期与是否采纳应由使用者核实。

`catalog` 指向 JSON 对象，包含 `entries` 数组。每项需要 `name`、`title`、`category`、`url`、`license`、`status` 和 `installed_path`。只有 `status=installed_verified` 才进入已连接集合；读取的是授权目录中的 `SKILL.md`，拒绝解析后越界的正文路径。这里阅读方法文本，不运行其安装指令、脚本或附带程序。

检索器配置应含 `database`（SQLite 路径）及 `collections` 数组。工作台连接工具新增一项：

```json
{"id":"growth-desk-notes","path":"/your/private/data/knowledge","evidence":"user_written_or_saved_conversation","status":"dated_record_not_automatically_verified_fact"}
```

已有检索器命令协议：`python3 advisor.py --config CONFIG build`，stdout 返回 JSON，`errors` 为空且退出码为零才继续校验。工作台另外核对本篇记录的索引 path 与实际文件 SHA-256，避免仅凭命令成功宣称已经入库。

读取数据库需有：

- `docs`：`id, title, collection, status, path, sha256, evidence`。
- `passages`：`doc_id, part, text`。

搜索采用只读连接。工作台自有笔记始终按本地最新版本搜索和回读，防止外部旧索引覆盖新笔记。正在编辑的记录不会被上一轮索引任务误标为新版已入库。外部其他工具若直接读旧索引，在重新构建成功前仍可能读到旧版本；工作台界面会保留“已保存/入库中/失败/已入库”的区别。

没有现成检索器也能独立使用工作台的记录、目标、来源目录和 Codex 对话。公开包附带通用索引器与51份顾问缩编正文，不附私人索引或上游完整原包。新设备可以运行 `python3 tools/setup_knowledge.py --data-dir /your/private/data`，只为该工作台的笔记建立索引并保留默认随包顾问；已有外部连接时会拒绝静默重绑。来源目录可见与正文可读分别显示。
