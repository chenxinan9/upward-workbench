# macOS 桌面入口（补充版本）

桌面双击 `向上工作台.app`，会在独立窗口打开本机工作台。已运行的服务直接复用；冷启动通过用户常规 Terminal 会话启动后台服务，终端显示“工作台已启动”后该窗口可以关闭。关闭工作台窗口不停止后台服务，重新打开会回到相同数据。

需要 macOS、Python 3.11+、Xcode Command Line Tools。构建：

```sh
python3 desktop/install.py --data-dir /your/private/growth-data
```

源码仍在本项目；经哈希记录的运行副本部署到当前用户的 `Library/Application Support/向上成长顾问/程序`。实际目标、笔记和对话始终指向传入的唯一私人数据目录，不复制私人库。安装生成的 `desktop.json`/deployment.json 只在私人应用支持目录，不可加入公开包。

安装器拒绝覆盖已存在的目标 App。更新时应先退出桌面应用、停止有关后台服务、保留旧构建，再构建新入口，并核对运行副本哈希；修改开发源文件不会自动更新运行副本。

应用只加载固定本机地址；外部 http/https 来源在默认浏览器打开，不把第三方页面装进可访问本机的工作台窗口。没有修改系统隐私权限、开机启动或后台定时器。

v0.2 源码包含入口构建器、growth.py和51份顾问缩编。旧 v0.1.0-rc2 ZIP 不包含这些变更，旧22项验收不能自动证明本版入口。冷启动、原有服务复用与本机实际窗口应单独留证。

2026-10-06应用更名为向上工作台，使用desktop/assets/upward-icon.png。为兼容既有数据，应用Bundle ID与Application Support目录保留原值。
