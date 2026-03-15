"""
AstrBot qBittorrent Control Plugin - Migrated from nonebot_plugin_qbcontrol.

This plugin provides qBittorrent management features:
- Query torrents by keyword
- Add keywords to RSS subscription rules
- Delete and tag torrents
- Scheduled cleanup of completed keywords
"""

import logging
import random
from datetime import datetime, timedelta
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .api import QB

logger = logging.getLogger("astrbot")

# Session storage for user interactions
# Structure: {session_id: {"torrents": [...], "pending_keywords": [...], "timestamp": datetime}}
_sessions: dict[str, dict[str, Any]] = {}

# Session timeout in minutes
SESSION_TIMEOUT = 5


def _get_session_key(event: AstrMessageEvent) -> str:
    """Generate unique session key from event."""
    return f"{event.session_id}"


def _cleanup_expired_sessions() -> None:
    """Remove expired sessions."""
    now = datetime.now()
    expired = [
        k
        for k, v in _sessions.items()
        if (now - v.get("timestamp", now)).total_seconds() > SESSION_TIMEOUT * 60
    ]
    for k in expired:
        del _sessions[k]


class Main(Star):
    """Main class for qBittorrent control plugin."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        config = config or {}

        self.qb_url = str(config.get("qb_url", ""))
        self.qb_username = str(config.get("qb_username", "admin"))
        self.qb_password = str(config.get("qb_password", ""))
        self.rss_rule = str(config.get("rss_rule", "Sub"))
        self.enable_reset_job = bool(config.get("enable_reset_job", False))

        self._qb_client: QB | None = None
        self._reset_job_name = "qbcontrol_reset_keywords"

    async def _get_qb_client(self) -> QB:
        """Get or create qBittorrent client."""
        if self._qb_client is None:
            self._qb_client = QB(self)
        return self._qb_client

    async def initialize(self) -> None:
        """Initialize plugin and setup scheduled jobs."""
        if self.enable_reset_job:
            await self._setup_reset_job()
            logger.info("qBittorrent scheduled keyword cleanup enabled")
        else:
            logger.info("qBittorrent scheduled keyword cleanup disabled")

    async def _setup_reset_job(self) -> None:
        """Setup the scheduled keyword cleanup job."""
        cron = getattr(self.context, "cron_manager", None)
        if cron is None:
            logger.warning("CronManager not available, scheduled job not created")
            return

        # Delete existing job if any
        existing = await cron.list_jobs("basic")
        for job in existing:
            if job.name == self._reset_job_name:
                await cron.delete_job(job.job_id)

        # Random minute to avoid conflicts
        minute = random.randint(0, 59)

        async def _reset_handler() -> None:
            logger.info("Starting scheduled keyword cleanup")
            client = await self._get_qb_client()
            await client.update_keywords(self.rss_rule)

        await cron.add_basic_job(
            name=self._reset_job_name,
            cron_expression=f"{minute} * * * *",  # Every hour at random minute
            handler=_reset_handler,
            description="qBittorrent: 清理已完成的订阅关键词",
            enabled=True,
            persistent=False,
        )
        logger.info(f"Scheduled keyword cleanup job registered (minute {minute})")

    async def terminate(self) -> None:
        """Cleanup when plugin is disabled."""
        if self._qb_client:
            await self._qb_client.close()
            self._qb_client = None

    @filter.command("qb")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qb_command(self, event: AstrMessageEvent) -> None:
        """qBittorrent management command.

        Usage:
            /qb [keyword1,keyword2,...] - Query torrents and manage subscriptions
        """
        _cleanup_expired_sessions()

        message = event.message_str.strip()
        keyword_text = message.replace("qb", "", 1).strip()

        client = await self._get_qb_client()

        # If no keyword provided, show usage
        if not keyword_text:
            yield event.plain_result(
                "用法: /qb <关键词1,关键词2,...>\n"
                "功能:\n"
                "  - 查询包含关键词的种子\n"
                "  - 未找到种子时，可将关键词添加到RSS订阅规则"
            )
            return

        try:
            keywords = [kw.strip() for kw in keyword_text.split(",") if kw.strip()]
            torrent_list: list[dict] = []
            pending_keywords: list[str] = []
            info_msgs: list[str] = []

            rule, current_expr = await client.get_rules(self.rss_rule)

            for kw in keywords:
                try:
                    torrents = await client.search_torrents_list(kw)
                except Exception as e:
                    info_msgs.append(f"❌ 查询 '{kw}' 失败: {e}")
                    logger.error(f"Failed to query torrent {kw}: {e}")
                    continue

                if torrents:
                    torrent_list.extend(torrents)
                else:
                    if kw in current_expr:
                        info_msgs.append(f"ℹ️ '{kw}' 已存在于规则中")
                    else:
                        pending_keywords.append(kw)

            # Build response message
            text_parts = info_msgs.copy()

            # Create numbered menu items
            menu_items: list[
                dict
            ] = []  # Each item: {"type": "torrent|keyword", "data": {...}}

            if torrent_list:
                text_parts.append(f"\n📦 查询到 {len(torrent_list)} 个种子:")
                for i, t in enumerate(torrent_list, start=1):
                    name_display = (
                        t["name"][:30] + "..." if len(t["name"]) > 30 else t["name"]
                    )
                    text_parts.append(
                        f"  {i}. {name_display}\n"
                        f"     🏁 Tracker: {t['tracker']}\n"
                        f"     🕐 完成: {t['complete']}"
                    )
                    menu_items.append({"type": "torrent", "data": t, "index": i})

            if pending_keywords:
                text_parts.append(
                    f"\n📝 发现 {len(pending_keywords)} 个新关键词待订阅:"
                )
                for i, kw in enumerate(pending_keywords, start=len(torrent_list) + 1):
                    text_parts.append(f"  {i}. ➕ 添加订阅: {kw}")
                    menu_items.append(
                        {"type": "keyword", "data": {"keyword": kw}, "index": i}
                    )

            if not torrent_list and not pending_keywords and not info_msgs:
                text_parts.append("未找到相关种子，也没有新关键词需要订阅")

            # Add instruction for reply
            if menu_items:
                text_parts.append(
                    "\n💡 回复序号执行操作，或回复 'd序号' 删除，'t序号' 打标签"
                )
                text_parts.append("   例如: '1' 查看详情, 'd1' 删除第1个, 't1' 打标签")

            # Store session data
            session_key = _get_session_key(event)
            _sessions[session_key] = {
                "torrents": torrent_list,
                "pending_keywords": pending_keywords,
                "menu_items": menu_items,
                "timestamp": datetime.now(),
            }

            yield event.plain_result("\n".join(text_parts))

        except Exception as e:
            logger.error(f"qBittorrent command error: {e}")
            yield event.plain_result(f"❌ 操作失败: {e}")

    @filter.command("qb_add")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qb_add_command(self, event: AstrMessageEvent) -> None:
        """Directly add keywords to RSS subscription.

        Usage: /qb_add <keyword1,keyword2,...>
        """
        message = event.message_str.strip()
        keyword_text = message.replace("qb_add", "", 1).strip()

        if not keyword_text:
            yield event.plain_result("用法: /qb_add <关键词1,关键词2,...>")
            return

        client = await self._get_qb_client()
        keywords = [kw.strip() for kw in keyword_text.split(",") if kw.strip()]

        try:
            rule, current_expr = await client.get_rules(self.rss_rule)
            added = []

            for kw in keywords:
                if kw not in current_expr:
                    current_expr.append(kw)
                    added.append(kw)

            if added:
                rule["mustContain"] = "|".join(sorted(set(current_expr)))
                await client.set_rule(self.rss_rule, rule)
                yield event.plain_result(
                    f"✅ 已添加 {len(added)} 个关键词到订阅规则\n"
                    f"新增: {', '.join(added)}\n"
                    f"当前规则: `{rule['mustContain']}`"
                )
            else:
                yield event.plain_result("ℹ️ 所有关键词已存在于规则中")

        except Exception as e:
            logger.error(f"Failed to add keywords: {e}")
            yield event.plain_result(f"❌ 添加失败: {e}")

    @filter.command("qb_del")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qb_del_command(self, event: AstrMessageEvent) -> None:
        """Remove keywords from RSS subscription.

        Usage: /qb_del <keyword1,keyword2,...>
        """
        message = event.message_str.strip()
        keyword_text = message.replace("qb_del", "", 1).strip()

        if not keyword_text:
            yield event.plain_result("用法: /qb_del <关键词1,关键词2,...>")
            return

        client = await self._get_qb_client()
        keywords = [kw.strip() for kw in keyword_text.split(",") if kw.strip()]

        try:
            rule, current_expr = await client.get_rules(self.rss_rule)
            removed = []

            for kw in keywords:
                if kw in current_expr:
                    current_expr.remove(kw)
                    removed.append(kw)

            if removed:
                rule["mustContain"] = "|".join(sorted(set(current_expr)))
                await client.set_rule(self.rss_rule, rule)
                yield event.plain_result(
                    f"✅ 已移除 {len(removed)} 个关键词\n"
                    f"移除: {', '.join(removed)}\n"
                    f"当前规则: `{rule['mustContain']}`"
                )
            else:
                yield event.plain_result("ℹ️ 未找到要移除的关键词")

        except Exception as e:
            logger.error(f"Failed to remove keywords: {e}")
            yield event.plain_result(f"❌ 移除失败: {e}")

    @filter.command("qb_list")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qb_list_command(self, event: AstrMessageEvent) -> None:
        """List current RSS subscription keywords."""
        client = await self._get_qb_client()

        try:
            rule, current_expr = await client.get_rules(self.rss_rule)

            if current_expr:
                yield event.plain_result(
                    f"📋 当前订阅规则 '{self.rss_rule}' 的关键词:\n"
                    + "\n".join(f"  • {kw}" for kw in sorted(current_expr))
                    + f"\n\n共 {len(current_expr)} 个关键词"
                )
            else:
                yield event.plain_result(f"📋 订阅规则 '{self.rss_rule}' 暂无关键词")

        except Exception as e:
            logger.error(f"Failed to list keywords: {e}")
            yield event.plain_result(f"❌ 查询失败: {e}")

    @filter.command("qb_clean")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qb_clean_command(self, event: AstrMessageEvent) -> None:
        """Clean up keywords that have matching torrents."""
        client = await self._get_qb_client()

        try:
            await client.update_keywords(self.rss_rule)
            rule, current_expr = await client.get_rules(self.rss_rule)
            yield event.plain_result(
                f"✅ 清理完成\n当前剩余 {len(current_expr)} 个关键词"
            )
        except Exception as e:
            logger.error(f"Failed to clean keywords: {e}")
            yield event.plain_result(f"❌ 清理失败: {e}")


# Handle number replies for interactive actions
# This is a catch-all handler that processes session-based replies
class QBReplyHandler(Star):
    """Handler for processing numbered replies in qBittorrent sessions."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self._qb_client: QB | None = None

    async def _get_qb_client(self) -> QB:
        """Get or create qBittorrent client."""
        # Reuse the main plugin's client
        config = self.config or {}

        # Create a simple config object that mimics the original
        class Config:
            def __init__(self, cfg: dict):
                self.qb_url = cfg.get("qb_url", "")
                self.qb_username = cfg.get("qb_username", "admin")
                self.qb_password = cfg.get("qb_password", "")
                self.rss_rule = cfg.get("rss_rule", "Sub")

        if self._qb_client is None:
            self._qb_client = QB(Config(config))
        return self._qb_client

    @filter.message_filter()
    async def handle_reply(self, event: AstrMessageEvent) -> None:
        """Handle numbered replies for interactive actions."""
        _cleanup_expired_sessions()

        message = event.message_str.strip()
        session_key = _get_session_key(event)

        # Check if session exists
        if session_key not in _sessions:
            return  # Not an interactive session, skip

        session = _sessions[session_key]
        menu_items = session.get("menu_items", [])

        if not menu_items:
            return

        # Parse the reply
        action = "view"  # default action
        num_str = message

        if message.startswith("d") or message.startswith("D"):
            action = "delete"
            num_str = message[1:]
        elif message.startswith("t") or message.startswith("T"):
            action = "tag"
            num_str = message[1:]

        try:
            index = int(num_str)
        except ValueError:
            return  # Not a valid number, skip

        # Find the menu item
        item = None
        for mi in menu_items:
            if mi.get("index") == index:
                item = mi
                break

        if not item:
            yield event.plain_result(f"❌ 无效序号: {index}")
            return

        client = await self._get_qb_client()

        try:
            if item["type"] == "torrent":
                torrent = item["data"]

                if action == "view":
                    # Show torrent details with options
                    yield event.plain_result(
                        f"📌 {torrent['name']}\n\n"
                        f"🏁 Tracker: {torrent['tracker']}\n"
                        f"🕐 完成时间: {torrent['complete']}\n"
                        f"🔑 Hash: `{torrent['hash']}`\n\n"
                        f"💡 回复 'd{index}' 删除，'t{index}' 打标签"
                    )
                    return

                elif action == "delete":
                    status = await client.delete_torrents(torrent["hash"])
                    if status:
                        # Remove from session
                        session["torrents"] = [
                            t
                            for t in session["torrents"]
                            if t["hash"] != torrent["hash"]
                        ]
                        session["menu_items"] = [
                            mi for mi in menu_items if mi.get("index") != index
                        ]
                        yield event.plain_result(f"✅ 已删除: {torrent['name'][:30]}")
                    else:
                        yield event.plain_result("❌ 删除失败")

                elif action == "tag":
                    # Generate tag name based on completion time
                    tag_name = ""
                    if torrent["complete"] != "未完成":
                        try:
                            completion_date = datetime.strptime(
                                torrent["complete"], "%Y-%m-%d %H:%M:%S"
                            )
                            new_date = completion_date + timedelta(days=3)
                            tag_name = new_date.strftime("%m-%d")
                        except ValueError:
                            pass

                    if not tag_name:
                        tag_name = (datetime.now() + timedelta(days=3)).strftime(
                            "%m-%d"
                        )

                    status = await client.tag_torrents(torrent["hash"], tag_name)
                    if status:
                        yield event.plain_result(f"✅ 已添加标签: {tag_name}")
                    else:
                        yield event.plain_result("❌ 添加标签失败")

            elif item["type"] == "keyword":
                keyword = item["data"]["keyword"]

                if action == "view":
                    # Add keyword to RSS subscription
                    rss_rule = (
                        self.config.get("rss_rule", "Sub") if self.config else "Sub"
                    )
                    rule, current_expr = await client.get_rules(rss_rule)

                    if keyword not in current_expr:
                        current_expr.append(keyword)
                        rule["mustContain"] = "|".join(sorted(set(current_expr)))
                        await client.set_rule(rss_rule, rule)

                        # Remove from pending
                        session["pending_keywords"].remove(keyword)
                        session["menu_items"] = [
                            mi for mi in menu_items if mi.get("index") != index
                        ]

                        yield event.plain_result(
                            f"✅ 已添加 '{keyword}' 到订阅规则\n"
                            f"当前规则: `{rule['mustContain']}`"
                        )
                    else:
                        yield event.plain_result(f"ℹ️ '{keyword}' 已存在于规则中")

                else:
                    yield event.plain_result(
                        "⚠️ 关键词只支持查看操作（添加到订阅）\n回复序号即可添加"
                    )

        except Exception as e:
            logger.error(f"Failed to process reply action: {e}")
            yield event.plain_result(f"❌ 操作失败: {e}")
