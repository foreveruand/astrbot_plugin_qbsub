# AstrBot qBittorrent 管理插件

从 `nonebot_plugin_qbcontrol` 迁移到 AstrBot 的 qBittorrent 管理插件。

## 功能特性

- 🔍 **查询种子**: 通过关键词查询 qBittorrent 中的种子
- ➕ **RSS 订阅管理**: 将关键词添加到 RSS 自动下载规则
- 🗑️ **删除种子**: 删除指定的种子及其文件
- 🏷️ **打标签**: 为种子添加标签（默认为完成日期 +3 天）
- ⏰ **定时清理**: 自动清理已匹配到种子的订阅关键词
- 📱 **Telegram 内联键盘**: 在 Telegram 上直接点击按钮执行查看、删除、打标签和添加订阅

## 指令列表

| 命令 | 说明 | 示例 |
|------|------|------|
| `/qb [关键词]` | 查询种子并管理订阅 | `/qb movie1,movie2` |
| `/qb_list` | 列出当前订阅关键词 | `/qb_list` |

## 交互式操作

`/qb` 命令支持交互式操作：

```
📦 查询到 2 个种子
  1. movie1
     🏁 Tracker: tracker1
     🕐 完成: 2024-01-01 12:00:00
  2. movie2
     🏁 Tracker: tracker2
     🕐 完成: 未完成

📝 发现 1 个新关键词待订阅
  3. ➕ 添加订阅: keyword1

💡 回复序号执行操作，或回复 'd序号' 删除，'t序号' 打标签
   例如: '1' 查看详情, 'd1' 删除第1个, 't1' 打标签
```

回复数字可执行相应操作：
- `1` - 查看种子详情
- `d1` - 删除第 1 个种子
- `t1` - 为第 1 个种子打标签
- `3` - 将关键词添加到订阅规则

Telegram 平台会在结果消息下方显示内联按钮：
- `查看` - 打开种子详情
- `删除` - 删除种子
- `打标` - 为种子添加标签
- `添加` - 将关键词加入订阅规则
- `取消` - 结束当前交互

## 配置说明

在 AstrBot 管理面板中配置以下选项：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `qb_url` | "" | qBittorrent WebUI 地址（必填） |
| `qb_username` | "admin" | 用户名 |
| `qb_password` | "" | 密码（必填） |
| `rss_rule` | "Sub" | RSS 规则名称 |
| `enable_reset_job` | false | 启用定时清理任务 |

## 依赖

- `httpx` - 异步 HTTP 客户端

## 迁移说明

本插件从 `nonebot_plugin_qbcontrol` 迁移而来，主要变化：

1. **框架迁移**: 从 NoneBot 迁移到 AstrBot Star API
2. **交互优化**: Telegram 平台恢复内联键盘，其他平台继续使用文本菜单 + 序号回复
3. **会话控制适配**: 交互流程使用 AstrBot 官方 `session_waiter`，并按发送者自定义会话过滤器适配平台事件差异
4. **调度系统**: 使用 AstrBot 内置 CronJobManager 替代 APScheduler
5. **配置方式**: 使用 AstrBot 可视化配置面板

## 许可证

MIT License
