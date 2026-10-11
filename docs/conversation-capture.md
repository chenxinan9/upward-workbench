# 通过对话整理目标与记录

在“与 Codex 协作”直接输入并点击一次“发送”。默认携带进行中与暂停目标，开启“整理目标与记录”；查看发送依据为可选步骤，内部仍先建立预览快照再发送。仅讨论可取消整理选项，或明确说“只讨论，不保存”。回复正文与实际写入回执分开，只有回执证明写入结果。

明确本人采用的目标进入正在推进；犹豫和建议保留为候选。模型建议的步骤不会自动成为本人已采用行动。没有明确日期就保留未排期。保存记录、笔记入库与现实行动完成是不同状态。

## 两种结构及边界

工作台模型回复结构与桌面提交 API 不相同，不能直接互换。

| 入口 | 接受结构 | 作用 |
| --- | --- | --- |
| 工作台模型回复 | `{"answer":"正文","items":[]}`，最多 8 项 | 本轮原话提取；允许模型提出已有记录更新，由服务器核验后转换 |
| `tools/capture_conversation.py` / capture API | `request_id/source/source_text/items`，1—30 项 | 提交已整理的 `goal/action/note`；不接受 `update_action/update_goal` |

工作台更新仅能引用本轮发送快照 `active-goals` 中的目标、行动 ID 与版本。该快照包括进行中与暂停目标，不包含整库所有状态。模型不能任意提供新版本或把未附上的记录当作更新对象。

已有行动的模型项：

```json
{
  "key": "u1",
  "kind": "update_action",
  "target_goal": "本轮快照目标ID",
  "target_action": "本轮快照行动ID",
  "change": "schedule",
  "source_quote": "把拍摄练习安排到明天",
  "date_text": "明天"
}
```

`change` 支持 `complete/reopen/schedule`。`date_text` 仅排期时使用，接受原话中的今天、明天、后天或 YYYY-MM-DD。相对日期按任务创建时刻和配置时区换算，同一来源句中出现多个不同日期会拒绝写入。

已有目标使用 `kind=update_goal`、`target_goal`、`change=pause/resume/complete`、`source_quote`。完成须明确指向整个目标，不能把一步完成当成整个目标完成。行动完成和目标完成均记录本人自报原话，不代表外部验收。

原话必须是本轮消息的连续逐字片段，并指明实际名称。服务器保守拒绝否定、假设、转述、提问、模糊指代、复杂多对象、同名歧义与没有明确日期的排期；明确“还没完成”的更正可恢复待做，但否定恢复不会执行。跨目标同名行动需在原话说明目标；同一目标内重复行动名称仍拒绝，须先区分名称。无快照、未知 ID、归属错误、旧版本冲突均不写原记录。复杂自然语言可能需要拆成单条明确指令，失败原因以回执为准。

## 桌面提交接口

桌面 Codex 可通过 CLI 向本机工作台提交已整理内容。接口不监听或读取其他桌面聊天，工作台会话与桌面聊天分开。

```sh
python3 tools/capture_conversation.py recent --limit 10
python3 tools/capture_conversation.py save --file /path/to/private/capture.json
python3 tools/capture_conversation.py status REQUEST_ID
```

顶层必填 `request_id`、`source`（`desktop_codex/workbench_conversation`）、`source_text`、`items`；可选 `source_id/source_time`。每项需唯一 `key`、`kind`（`goal/action/note`）、`title`、`source_quote`，原话必须是 `source_text` 的连续子串。未知字段拒绝，不接受任意数据库补丁。

| 类型 | 专属可用字段与约束 |
| --- | --- |
| goal | `why/domain/domain_id/priority/parent_id/status/evidence`；完成状态需要结果证据 |
| action | `goal_id/goal_rev` 或 `goal_ref` 二选一，再加 `criterion/outcome/done`；`done=true` 需要结果文字 |
| note | `body/index/goal_id/note_kind`；保存和索引分别返回状态 |

明确采用使用布尔值 `explicit=true/adopted=true`。新建目标的步骤单列为行动；`goal_ref` 只能引用同批前置目标的 `key`。新建不接受 `rev`。日期统一使用 YYYY-MM-DD；此直接提交接口不自动理解口语日期。

更新既有目标或笔记需实际 `id/rev`。更新行动需实际行动 `id`、父目标 `goal_id/goal_rev`，不能填行动 `rev` 或 `goal_ref`。更新既有目标、行动还须明确采用；同名不会静默覆盖，不支持靠标题猜 ID。直接提交方负责先核对原话与授权，再获取实际版本；工作台模型输出的自然语言防歧义规则不能被假定为这个接口的通用语义判断器。

## 回执、重试与留底

相同请求重试沿用同一个 `request_id` 和原提交内容。保存后 `state.captures` 与聊天回执显示实际条目；行动回执含 `due/done`，行动定位入口在确认行动存在后直接打开该行动的日期与结果；旧回执暂时无法匹配行动时回退到所属目标。保存与笔记索引是独立状态，未入库不应报告已可检索。

部分失败保留已成功部分。有有效整理结构时可只重试写入，不重新调用模型；回复缺少有效结构时保留正文，使用手动取舍入口，不假称成功。网络发送结果不明使用明确重试入口沿用旧快照；普通“发送”不会暗中替用户重发旧问题。新问题与原会话草稿仍保留。
