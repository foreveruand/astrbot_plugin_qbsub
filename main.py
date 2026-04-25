import logging
import random
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.util import SessionController, session_waiter
from astrbot.core.platform.sources.telegram.tg_event import TelegramCallbackQueryEvent
from astrbot.core.utils.session_waiter import (
    USER_SESSIONS,
    SessionFilter,
    SessionWaiter,
)

from .api import QB

logger = logging.getLogger("astrbot")

SESSION_TIMEOUT = 60
QBSUB_CALLBACK_PREFIX = "qbsub"


class SenderSessionFilter(SessionFilter):
    """Use sender as session key to avoid platform UMO drift in callbacks/messages."""

    def filter(self, event: AstrMessageEvent) -> str:
        sender_id = event.get_sender_id()
        platform_id = event.get_platform_id()
        if sender_id:
            return f"{platform_id}:{sender_id}"
        return event.unified_msg_origin


class Main(Star):
    """Main class for qBittorrent control plugin."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)

        self.qb_url = str(config.get("qb_url", ""))
        self.qb_username = str(config.get("qb_username", "admin"))
        self.qb_password = str(config.get("qb_password", ""))
        self.rss_rule = str(config.get("rss_rule", "Sub"))
        self.enable_reset_job = bool(config.get("enable_reset_job", False))

        self._qb_client: QB | None = None
        self._reset_job_name = "qBittorrent Keyword Cleanup Job"
        self._cron_job_id: str | None = None

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

        existing = await cron.list_jobs("basic")
        for job in existing:
            if job.name == self._reset_job_name:
                await cron.delete_job(job.job_id)

        minute = random.randint(0, 59)

        async def _reset_handler() -> None:
            logger.info("Starting scheduled keyword cleanup")
            client = await self._get_qb_client()
            await client.update_keywords(self.rss_rule)

        job = await cron.add_basic_job(
            name=self._reset_job_name,
            cron_expression=f"{minute} * * * *",
            handler=_reset_handler,
            description="qBittorrent: 清理已完成的订阅关键词",
            enabled=True,
            persistent=False,
        )
        self._cron_job_id = job.job_id
        logger.info(f"Scheduled keyword cleanup job registered (minute {minute})")

    async def terminate(self) -> None:
        """Cleanup when plugin is disabled."""
        if self._qb_client:
            await self._qb_client.close()
            self._qb_client = None
        if self._cron_job_id:
            cron = getattr(self.context, "cron_manager", None)
            if cron:
                try:
                    await cron.delete_job(self._cron_job_id)
                    logger.info("Scheduled keyword cleanup job removed")
                except Exception as e:
                    logger.warning(f"Failed to remove scheduled job: {e}")

    def _build_inline_keyboard(
        self, menu_items: list[dict[str, Any]]
    ) -> list[list[dict]]:
        keyboard: list[list[dict[str, str]]] = []
        for item in menu_items:
            index = item["index"]
            if item["type"] == "torrent":
                keyboard.append(
                    [
                        {
                            "text": f"{index} 查看",
                            "callback_data": f"{QBSUB_CALLBACK_PREFIX}:view:{index}",
                        },
                        {
                            "text": f"{index} 删除",
                            "callback_data": f"{QBSUB_CALLBACK_PREFIX}:delete:{index}",
                        },
                        {
                            "text": f"{index} 打标",
                            "callback_data": f"{QBSUB_CALLBACK_PREFIX}:tag:{index}",
                        },
                    ]
                )
            else:
                keyboard.append(
                    [
                        {
                            "text": f"{index} 添加",
                            "callback_data": f"{QBSUB_CALLBACK_PREFIX}:add:{index}",
                        }
                    ]
                )
        keyboard.append(
            [
                {
                    "text": "取消",
                    "callback_data": f"{QBSUB_CALLBACK_PREFIX}:cancel:0",
                }
            ]
        )
        return keyboard

    def _build_menu_text(
        self,
        menu_items: list[dict[str, Any]],
        info_msgs: list[str] | None = None,
        status_msgs: list[str] | None = None,
    ) -> str:
        text_parts = list(info_msgs or [])
        torrent_items = [item for item in menu_items if item.get("type") == "torrent"]
        keyword_items = [item for item in menu_items if item.get("type") == "keyword"]

        if torrent_items:
            text_parts.append(f"\n📦 查询到 {len(torrent_items)} 个种子:")
            for item in torrent_items:
                index = item["index"]
                torrent = item["data"]
                name_display = (
                    torrent["name"][:30] + "..."
                    if len(torrent["name"]) > 30
                    else torrent["name"]
                )
                text_parts.append(
                    f"  {index}. {name_display}\n"
                    f"     🏁 Tracker: {torrent['tracker']}\n"
                    f"     🕐 完成: {torrent['complete']}"
                )

        if keyword_items:
            text_parts.append(f"\n📝 发现 {len(keyword_items)} 个新关键词待订阅:")
            for item in keyword_items:
                index = item["index"]
                kw = item["data"]["keyword"]
                text_parts.append(f"  {index}. ➕ 添加订阅: {kw}")

        if status_msgs:
            text_parts.append("\n📣 最近操作:")
            for status in status_msgs[-3:]:
                text_parts.append(f"  {status}")

        if menu_items:
            text_parts.append(
                "\n💡 回复序号执行操作，或回复 'd序号' 删除，'t序号' 打标签"
            )
            text_parts.append("   例如: '1' 查看详情, 'd1' 删除第1个, 't1' 打标签")
        else:
            text_parts.append("未找到相关种子，也没有新关键词需要订阅")

        return "\n".join(text_parts)

    @filter.callback_query()
    async def handle_qbsub_callback(self, event: TelegramCallbackQueryEvent) -> None:
        """Bridge Telegram callback events into session_waiter for qbsub interactions."""
        data = (event.data or "").strip()
        if not data.startswith(f"{QBSUB_CALLBACK_PREFIX}:"):
            event.continue_event()
            return

        event.message_str = data
        session_id = SenderSessionFilter().filter(event)
        if session_id not in USER_SESSIONS:
            await event.answer_callback_query(text="会话已过期，请重新发送 /qb")
            event.stop_event()
            return

        await SessionWaiter.trigger(session_id, event)
        event.stop_event()

    @filter.command("qb")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qb_command(self, event: AstrMessageEvent, keyword: str):
        """qBittorrent management command.

        Usage:
            /qb [keyword1,keyword2,...] - Query torrents and manage subscriptions
        """
        keyword_text = keyword.strip()

        client = await self._get_qb_client()

        try:
            keywords = [kw.strip() for kw in keyword_text.split(",") if kw.strip()]
            torrent_list: list[dict[str, Any]] = []
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

            menu_items: list[dict[str, Any]] = []
            status_msgs: list[str] = []

            if torrent_list:
                for i, t in enumerate(torrent_list, start=1):
                    menu_items.append({"type": "torrent", "data": t, "index": i})

            if pending_keywords:
                for i, kw in enumerate(pending_keywords, start=len(torrent_list) + 1):
                    menu_items.append(
                        {"type": "keyword", "data": {"keyword": kw}, "index": i}
                    )

            if not menu_items:
                yield event.plain_result(self._build_menu_text(menu_items, info_msgs))
                return

            yield event.plain_result(
                self._build_menu_text(menu_items, info_msgs, status_msgs)
            ).inline_keyboard(self._build_inline_keyboard(menu_items))

            @session_waiter(timeout=SESSION_TIMEOUT)
            async def wait_for_reply(
                controller: SessionController, reply_event: AstrMessageEvent
            ):
                nonlocal menu_items, status_msgs

                is_callback = hasattr(reply_event, "callback_query_id")

                async def _send_status(text: str) -> None:
                    if is_callback:
                        status_msgs.append(text)
                        result = reply_event.plain_result(
                            self._build_menu_text(menu_items, info_msgs, status_msgs)
                        )
                        if menu_items:
                            result.inline_keyboard(
                                self._build_inline_keyboard(menu_items)
                            )
                        await reply_event.send(result)
                        return
                    await reply_event.send(reply_event.plain_result(text))

                msg = reply_event.message_str.strip()
                action = "view"
                num_str = msg

                if msg.lower() in ("取消", "cancel", "退出", "exit"):
                    await reply_event.send(reply_event.plain_result("已取消操作。"))
                    controller.stop()
                    return

                if msg.startswith(f"{QBSUB_CALLBACK_PREFIX}:"):
                    parts = msg.split(":", 2)
                    if len(parts) != 3:
                        await _send_status("⚠️ 无效的按钮数据")
                        controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                        return
                    _, action, num_str = parts
                    if action == "cancel":
                        await reply_event.send(reply_event.plain_result("已取消操作。"))
                        controller.stop()
                        return
                    if hasattr(reply_event, "callback_query_id"):
                        await reply_event.answer_callback_query()
                elif msg.startswith(("d", "D")):
                    action = "delete"
                    num_str = msg[1:]
                elif msg.startswith(("t", "T")):
                    action = "tag"
                    num_str = msg[1:]

                try:
                    index = int(num_str)
                except ValueError:
                    await _send_status("⚠️ 请输入有效的序号，或回复 '取消' 退出")
                    controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                    return

                item = None
                for mi in menu_items:
                    if mi.get("index") == index:
                        item = mi
                        break

                if not item:
                    await _send_status(f"❌ 无效序号: {index}")
                    controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                    return

                try:
                    if item["type"] == "torrent":
                        torrent = item["data"]

                        if action == "view":
                            detail_text = (
                                f"📌 {torrent['name']}\n\n"
                                f"🏁 Tracker: {torrent['tracker']}\n"
                                f"🕐 完成时间: {torrent['complete']}\n"
                                f"🔑 Hash: `{torrent['hash']}`\n\n"
                                f"💡 回复 'd{index}' 删除，'t{index}' 打标签"
                            )
                            if hasattr(reply_event, "callback_query_id") and hasattr(
                                reply_event, "client"
                            ):
                                chat = getattr(
                                    getattr(reply_event, "message", None), "chat", None
                                )
                                chat_id = getattr(chat, "id", None)
                                if chat_id is not None:
                                    await reply_event.client.send_message(
                                        chat_id=chat_id, text=detail_text
                                    )
                                else:
                                    await reply_event.send(
                                        reply_event.plain_result(detail_text)
                                    )
                            else:
                                await reply_event.send(
                                    reply_event.plain_result(detail_text)
                                )
                            controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                            return

                        if action == "delete":
                            status = await client.delete_torrents(torrent["hash"])
                            if status:
                                menu_items = [
                                    mi for mi in menu_items if mi.get("index") != index
                                ]
                                await _send_status(f"✅ 已删除: {torrent['name'][:30]}")
                            else:
                                await _send_status("❌ 删除失败")

                            if not menu_items:
                                controller.stop()
                            else:
                                controller.keep(
                                    timeout=SESSION_TIMEOUT, reset_timeout=True
                                )
                            return

                        if action == "tag":
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
                                tag_name = (
                                    datetime.now() + timedelta(days=3)
                                ).strftime("%m-%d")

                            status = await client.tag_torrents(
                                torrent["hash"], tag_name
                            )
                            if status:
                                await _send_status(f"✅ 已添加标签: {tag_name}")
                            else:
                                await _send_status("❌ 添加标签失败")
                            controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                            return

                    if item["type"] == "keyword":
                        keyword = item["data"]["keyword"]

                        if action not in ("view", "add"):
                            await _send_status(
                                "⚠️ 关键词只支持查看操作（添加到订阅）\n回复序号即可添加"
                            )
                            controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                            return

                        rule, current_expr = await client.get_rules(self.rss_rule)
                        if keyword not in current_expr:
                            current_expr.append(keyword)
                            rule["mustContain"] = "|".join(sorted(set(current_expr)))
                            await client.set_rule(self.rss_rule, rule)

                            menu_items = [
                                mi for mi in menu_items if mi.get("index") != index
                            ]
                            await _send_status(
                                f"✅ 已添加 '{keyword}' 到订阅规则\n"
                                f"当前规则: `{rule['mustContain']}`"
                            )
                        else:
                            await _send_status(f"ℹ️ '{keyword}' 已存在于规则中")

                        if not menu_items:
                            controller.stop()
                        else:
                            controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                        return

                    await _send_status("⚠️ 未知菜单项类型")
                    controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                    return

                except Exception as e:
                    logger.error(f"Failed to process reply action: {e}")
                    await _send_status(f"❌ 操作失败: {e}")
                    controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                    return

            try:
                await wait_for_reply(event, session_filter=SenderSessionFilter())
            except TimeoutError:
                yield event.plain_result("⏰ 等待超时，操作已取消。")
            finally:
                event.stop_event()

        except Exception as e:
            logger.error(f"qBittorrent command error: {e}")
            yield event.plain_result(f"❌ 操作失败: {e}")

    @filter.command("qb_list")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qb_list_command(self, event: AstrMessageEvent):
        """List current RSS subscription keywords."""
        client = await self._get_qb_client()

        try:
            _rule, current_expr = await client.get_rules(self.rss_rule)

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
