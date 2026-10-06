# 摄影与英语：完全虚构的复用示例

这些文件从零编写，不来自项目作者或其他真实用户的画像、计划或素材。所有示例日期、意愿和练习内容都是合成数据；没有真实目标或成果。

从项目根目录运行：

```sh
python3 examples/init_demo.py --data-dir "$HOME/UpwardDemoData"
python3 tools/setup_knowledge.py --data-dir "$HOME/UpwardDemoData"
python3 app.py --data-dir "$HOME/UpwardDemoData" --port 60129
```

打开 <http://127.0.0.1:60129>。初始化器只接受尚不存在、且在源码之外的目录；不会覆盖旧设置，不会创建已采用目标，不读取其他个人目录，不发送模型请求。第二条命令配置本地笔记索引，第三条启动服务。

文件对应关系：

| 文件 | 用途 |
| --- | --- |
| `growth-plan.sample.json` | 两个练习方向、四项候选行动、独立全景以及来源登记 |
| `sources/profile.md` | 合成学习者的方向，标明“不是实际用户” |
| `sources/project.md` | 候选练习方案，没有已执行记录 |
| `sources/method.md` | 通用练习反馈方法，不冒充名人原话 |
| `init_demo.py` | 将以上资料复制到新数据目录，生成本机设置 |

初始化后，`settings.json` 中的 `growth_plan` 指向复制出的 `growth-plan.json`，`private_root` 指向本次新目录。来源使用 `sources/*.md` 相对路径；页面上的“回读”可打开副本。源码中的 JSON 不含使用者绝对路径。读取来源时会比较 SHA-256，文件被改过会显示版本变化提醒。

替换成自己的资料时，编辑**数据目录里的副本**，不编辑仓库示例：

1. 先改 `sources/`，注明原话、日期和信息类型。
2. 按来源整理 `life_panorama`，区分意愿、事实、AI 助理建议与历史阶段。
3. 调整 `domains`、`known_facts` 和 `actions`；保留稳定 ID。已采用的目标另有来源快照，不应直接改旧快照。
4. 核对后更新 `source_register` 的路径、说明和哈希。不要只为消除提醒而更新哈希。
5. 修改后刷新页面；修改 `settings.json` 需要重启服务。

JSON 校验及示例来源哈希核对方法见 [上手说明](../docs/getting-started.md)。这个示例没有照片；工作台文字对话不会把照片像素发送给 AI 助理。
