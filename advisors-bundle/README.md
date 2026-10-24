# 可独立使用的顾问技能与参考资料包

本包实际包含 **56 份自包含 SKILL.md**：33 个人物视角、23 个功能顾问。每份具有不同的输入、方法、可检查输出和适用边界；配套 references 与 examples 按各入口指引读取，方法详解、练习与合成案例随包提供。这里不是60份原版镜像，也不包含名人本人或机构授权。

`manifest.json` 是工作台阅读入口，`methods.json` 登记37个跨顾问去重的方法子集。37不代表上游所有著作或全部技能的方法总数；同一方法可对应多个顾问。`excluded.json` 保留4个未分发条目及原因。

## 安装与使用

工作台可直接按manifest读取各目录SKILL.md，不必先把56份写进全局技能目录。需要在支持文件夹技能的Codex环境使用时，可先选择少量顾问。

从本目录执行以下命令。`$HOME` 只引用使用者现有目录，不改变环境配置：

```sh
python3 install.py --destination "$HOME/.codex/skills" --name think-woop
python3 install.py --destination "$HOME/.codex/skills" --name think-woop --apply
```

第一条只预览，不写文件；第二条执行复制。省略`--name`会选择全部56份。安装器验证文件哈希，拒绝覆盖已有同名目录；遇到已有技能请比较版本，或用另一个空目录试装。安装后让宿主刷新技能发现或开启新对话；文件复制完成与宿主已加载是两件事。可用 `$think-woop` 等名称调用，是否自动选择取决于宿主。

也可将单个技能目录连同 references、examples（如有）、LICENSE、LICENSE.*（如有）、NOTICE.md（如有）、provenance.json一起复制到自己的技能目录。不要只分享SKILL正文而丢掉归属和许可。

## 可复用内容与许可

- 50份MIT、5份Apache-2.0、1份CC-BY-4.0。逐项原许可保存在各目录LICENSE，固定提交和改动保存在provenance.json。
- 缩编版本剔除了上游长引文、人物传记断言、冒充本人提示、外部脚本、未附带参考路径和私人接入假设，重新组织为实用流程。新增参考资料为标明来源的改编，并非上游完整研究语料；不是宣称上游完整内容均可分发。
- informed-patient来自Dr. Cat Hicks；缩编保留CC-BY署名、许可与修改说明，不能改为本项目MIT。
- methods.json的方法文字是对应来源的标注缩编，逐条license_basis和source_urls给出归属。不要对全部条目统一覆盖许可。
- `career-coach`、`marx-perspective`许可未明确；`dbs-learning`有非商业限制；`relationship-science-coach`为内部使用条款，四者正文未分发。

## 验证范围

格式、引用、敏感路径模式、文件哈希及隔离安装属于包的完整性检查。它们是可执行的对话指引，效果仍需依据实际问题和宿主模型检查；不能把格式或安装检查当成56项模型咨询效果全部验收。新增内容和本版验证边界见工作台的版本说明。

此目录没有用户档案、历史目标、真实健康或财务数据、个人想法，也不含项目发起者的宣传视频策划。正式公开包应对包含本目录的具体ZIP再审计。
