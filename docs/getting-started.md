# 开始使用向上人生顾问团与工作台

## 一条命令开始

macOS / Linux 上安装 Python 3.11+ 后，运行：

```bash
curl -fsSL https://raw.githubusercontent.com/chenxinan9/upward-workbench/main/install.sh | bash
```

安装器会检查 SQLite 全文检索环境，下载固定的 v0.2.1-rc2 发布包，校验 SHA-256 和文件清单，准备虚构的摄影与英语示例、连接笔记索引并打开本机网页。顾问书房可直接阅读 51 份方法，不需要先登录 AI 账号。

程序默认放在 `~/.local/share/upward-workbench/app-v0.2.1-rc2`，示例数据独立放在同级 `demo-data`。地址使用自动选出的空闲端口，以终端输出为准。安装器不会覆盖已有目录，不会扫描其他私人资料，不会安装全局 Skill 或替你调用模型。[查看安装器源码](../install.sh)。

关闭网页后服务仍在本机运行。终端末尾会打印停止命令与再次启动命令；这两条命令也对应数据目录 `quickstart.json` 内的地址和路径。重复安装时若提示目录已存在，请继续使用原服务，或换一组新目录体验：

```bash
curl -fsSL https://raw.githubusercontent.com/chenxinan9/upward-workbench/main/install.sh | bash -s -- --install-dir "$HOME/UpwardApp" --data-dir "$HOME/UpwardDemo"
```

没有自动弹出浏览器时，手动打开终端输出的地址。需要 AI 讨论时连接 Codex CLI；需要 macOS 桌面图标时见[桌面安装](../desktop/README.md)。一键体验本身不安装原生桌面应用。

## 手动安装与配置

下载 [RC2 ZIP](https://github.com/chenxinan9/upward-workbench/releases/download/v0.2.1-rc2/upward-workbench-v0.2.1-rc2.zip) 并解压，进入 `growth-desk` 文件夹。也可克隆当前仓库：

```bash
git clone https://github.com/chenxinan9/upward-workbench.git
cd upward-workbench
```

以下命令在项目根目录执行。合成示例只包含摄影、英语与练习反馈流程，不含项目作者资料。运行需要 Python 3.11+；本地笔记入库需要 SQLite FTS5 的 `trigram` tokenizer。

## 1. 检查运行环境

```sh
python3 --version
python3 -c 'import sqlite3; c=sqlite3.connect(":memory:"); c.execute("CREATE VIRTUAL TABLE probe USING fts5(body, tokenize=trigram)"); print("SQLite FTS5 trigram 可用", sqlite3.sqlite_version)'
```

第二条失败时，使用带有相应 SQLite 功能的 Python 环境。可以先运行工作台保存与搜索自己的笔记，但不要把尚未建立的统一索引标成已经接通。

## 2. 初始化一份可回读的示例

选择**尚不存在**的源码外目录。目录已存在时换一个新名字；初始化器不覆盖旧数据。

```sh
python3 examples/init_demo.py --data-dir "$HOME/UpwardDemoData"
python3 tools/setup_knowledge.py --data-dir "$HOME/UpwardDemoData"
python3 app.py --data-dir "$HOME/UpwardDemoData" --port 60129
```

保持该终端中的服务运行，浏览器打开 <http://127.0.0.1:60129>。结束时在运行服务的终端按 `Ctrl+C`。端口被占用时改成其他空闲端口，并同步浏览器地址。

打开后先看“人生全景”，点击虚构原话下方的“回读”，应出现标有“合成学习者”的文本。然后去“目标与计划”展开一个摄影或英语候选方案；只有主动采用后才会出现已采用目标。示例不会预填成绩、打勾完成记录或长期达成比例。

没有示例的空工作台也可使用。改用一个自己的数据目录，跳过 `init_demo.py`，直接执行索引配置与启动命令即可。

## 3. 认识目录与设置

| 位置 | 内容 |
| --- | --- |
| 项目源码 | 网页、Python 程序、顾问缩编、公共示例及文档 |
| 指定的数据目录 | `settings.json`、`desk.sqlite`、自己的来源与方案、调用回执和笔记 |
| 数据目录的 `growth-plan.json` | 从示例复制出的全景与领域方案，可替换成自己的内容 |
| 数据目录的 `sources/` | 合成来源副本；页面回读按 `source_register` 找到对应文件 |
| 数据目录的 `knowledge/` | 工作台保存的 Markdown 笔记，默认知识集合的来源 |

`settings.json` 的 `growth_plan` 是方案 JSON 的绝对路径；`private_root` 是允许读取方案来源的根目录。来源登记中的 `path` 是相对这个根目录的路径。初始化器会按你的机器生成路径，公共示例里没有真实机器路径。

设置在启动时读取，改设置需要重启。方案及来源在读取时加载，修改后刷新页面即可。只修改源码不会自动更新已经安装的 macOS 桌面运行副本，重新构建方法见 [桌面说明](../desktop/README.md)。

## 4. 替换为自己的方向

编辑数据目录中的副本，保留仓库合成示例便于回归。先整理原文，再整理页面字段：

- `source_register`：每条来源的稳定 `id`、标题、日期、相对路径、证据类型与 SHA-256。
- `life_panorama`：本人愿景 `north_star`、生活方向 `pillars`、阶段路线 `route`、成长方法 `mastery`、边界 `boundaries` 与历史 `history`。
- `domains`：当前可考虑的领域；每项含依据 `known_facts`、未知项、候选阶段方案 `plan_draft`、动作 `actions` 和顾问关联 `advisor_slugs`。
- `current_focus_draft.suggested_action_ids`：没有已采用目标时可提示的少量候选行动，引用 `actions` 中的 ID，不会自动采用。

可以只填写确有依据的部分。本人原话、历史愿望、AI 助理建议和已验证事实分别标明，旧情况发生变化时保留时间与修订原因。改方案不会改写已有目标的采用快照；已采用目标在目标页调整。

检查 JSON：

```sh
python3 -m json.tool "$HOME/UpwardDemoData/growth-plan.json" > /dev/null
```

以下命令只报告来源哈希是否一致，不替你更新判断或覆盖哈希：

```sh
python3 - "$HOME/UpwardDemoData" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1]).expanduser().resolve()
settings = json.loads((root / 'settings.json').read_text())
plan = json.loads(Path(settings['growth_plan']).read_text())
source_root = Path(settings['private_root']).resolve()
for item in plan['source_register']:
    if item.get('root') != 'private':
        continue
    file = (source_root / item['path']).resolve()
    if source_root not in file.parents:
        raise SystemExit('来源越界：' + item['id'])
    actual = hashlib.sha256(file.read_bytes()).hexdigest()
    print(item['id'], '一致' if actual == item.get('sha256') else '已变化，需要核对', actual)
PY
```

只有重新核对原文和相应解释后，才在数据目录的 `source_register` 中更新哈希。回读源码文件并不执行其中的指令。

## 5. 保存、搜索与统一入库

“保存”将想法留在工作台，并写入本地笔记。“保存并入库”还请求已配置的索引器重建并校验此条记录。完成后搜索记录中特有的词，点开回读确认内容；失败时原记录仍可保存，修复配置后重试。

`tools/setup_knowledge.py` 默认配置此数据目录的笔记集合，不扫描别处。示例 `sources/` 的三份材料可直接回读，但没有因为展示在全景里就自动加入全文检索。需要更多集合时，按本地检索配置显式添加，见 [接入接口](integration.md)。

## 6. 连接 AI 助理与桌面入口

AI 对话需要已安装并登录的 Codex CLI。先运行 `codex exec --help`，再重启工作台让它发现程序；也可在私人设置中配置 `codex_cli` 的绝对路径。到“与 Codex 协作”写问题、选资料、查看发送预览，再确认发送。状态显示“程序可用”不等于登录成功或模型已返回。

AI 助理当前处理文字，不直接读取未选资料、照片或整台电脑。每次实际发送遵循预览内容，调用可能产生费用。工具限制、取消边界与数据说明见 [安全说明](../SECURITY.md)。

macOS 用户可按 [桌面安装说明](../desktop/README.md) 构建“向上工作台.app”。浏览器模式不需要 Xcode；构建桌面入口需要 Xcode Command Line Tools。

## 常见问题

| 现象 | 先检查什么 |
| --- | --- |
| 人生全景提示尚未接入 | 是否用正确的 `--data-dir` 启动；`settings.json` 的 `growth_plan` 是否存在 |
| 来源无法回读 | `private_root`、相对路径、文件后缀及文件是否存在；可读文本类型为 `.md`、`.txt`、`.json`、`.docx` |
| 来源变化提醒 | 文件已与登记哈希不同，重新核对，而非忽略提醒 |
| 保存成功但入库失败 | 索引器配置、FTS5 环境和文件权限；不要重复新建同一条记录 |
| 能阅读顾问但 AI 对话失败 | 阅读本地方法不需要模型；对话需要登录有效的 CLI 与可用模型配置 |
| 改了源码，桌面仍是旧界面 | 桌面运行副本还没更新，按桌面安装流程重新构建并核对数据目录 |
| 日期没有安排 | 未排期行动不会自动算作今天；在行动的“安排与结果”中设置日期 |
