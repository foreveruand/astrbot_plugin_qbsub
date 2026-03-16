import logging
import random
from datetime import datetime, timedelta

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.util import SessionController, session_waiter

from .api import QB

logger = logging.getLogger("astrbot")

SESSION_TIMEOUT = 60


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

    @filter.command("qb")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qb_command(self, event: AstrMessageEvent):
        """qBittorrent management command.

        Usage:
            /qb [keyword1,keyword2,...] - Query torrents and manage subscriptions
        """
        message = event.message_str.strip()
        keyword_text = message.replace("qb", "", 1).strip()

        client = await self._get_qb_client()

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

            text_parts = info_msgs.copy()

            menu_items: list[dict] = []

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
                return

            if menu_items:
                text_parts.append(
                    "\n💡 回复序号执行操作，或回复 'd序号' 删除，'t序号' 打标签"
                )
                text_parts.append("   例如: '1' 查看详情, 'd1' 删除第1个, 't1' 打标签")

            yield event.plain_result("\n".join(text_parts))

            if not menu_items:
                return

            @session_waiter(timeout=SESSION_TIMEOUT)
            async def wait_for_reply(
                controller: SessionController, event: AstrMessageEvent
            ):
                nonlocal menu_items, torrent_list, pending_keywords

                msg = event.message_str.strip()

                if msg.lower() in ("取消", "cancel", "退出", "exit"):
                    await event.send(event.plain_result("已取消操作。"))
                    controller.stop()
                    return

                action = "view"
                num_str = msg

                if msg.startswith("d") or msg.startswith("D"):
                    action = "delete"
                    num_str = msg[1:]
                elif msg.startswith("t") or msg.startswith("T"):
                    action = "tag"
                    num_str = msg[1:]

                try:
                    index = int(num_str)
                except ValueError:
                    await event.send(
                        event.plain_result("⚠️ 请输入有效的序号，或回复 '取消' 退出")
                    )
                    return

                item = None
                for mi in menu_items:
                    if mi.get("index") == index:
                        item = mi
                        break

                if not item:
                    await event.send(event.plain_result(f"❌ 无效序号: {index}"))
                    return

                try:
                    if item["type"] == "torrent":
                        torrent = item["data"]

                        if action == "view":
                            await event.send(
                                event.plain_result(
                                    f"📌 {torrent['name']}\n\n"
                                    f"🏁 Tracker: {torrent['tracker']}\n"
                                    f"🕐 完成时间: {torrent['complete']}\n"
                                    f"🔑 Hash: `{torrent['hash']}`\n\n"
                                    f"💡 回复 'd{index}' 删除，'t{index}' 打标签"
                                )
                            )
                            return

                        elif action == "delete":
                            status = await client.delete_torrents(torrent["hash"])
                            if status:
                                torrent_list = [
                                    t
                                    for t in torrent_list
                                    if t["hash"] != torrent["hash"]
                                ]
                                menu_items = [
                                    mi for mi in menu_items if mi.get("index") != index
                                ]
                                await event.send(
                                    event.plain_result(
                                        f"✅ 已删除: {torrent['name'][:30]}"
                                    )
                                )
                            else:
                                await event.send(event.plain_result("❌ 删除失败"))
                            if not menu_items:
                                controller.stop()
                            return

                        elif action == "tag":
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
                                await event.send(
                                    event.plain_result(f"✅ 已添加标签: {tag_name}")
                                )
                            else:
                                await event.send(event.plain_result("❌ 添加标签失败"))
                            return

                    elif item["type"] == "keyword":
                        keyword = item["data"]["keyword"]

                        if action == "view":
                            rule, current_expr = await client.get_rules(self.rss_rule)

                            if keyword not in current_expr:
                                current_expr.append(keyword)
                                rule["mustContain"] = "|".join(
                                    sorted(set(current_expr))
                                )
                                await client.set_rule(self.rss_rule, rule)

                                pending_keywords.remove(keyword)
                                menu_items = [
                                    mi for mi in menu_items if mi.get("index") != index
                                ]

                                await event.send(
                                    event.plain_result(
                                        f"✅ 已添加 '{keyword}' 到订阅规则\n"
                                        f"当前规则: `{rule['mustContain']}`"
                                    )
                                )
                            else:
                                await event.send(
                                    event.plain_result(f"ℹ️ '{keyword}' 已存在于规则中")
                                )

                            if not menu_items:
                                controller.stop()
                            return

                        else:
                            await event.send(
                                event.plain_result(
                                    "⚠️ 关键词只支持查看操作（添加到订阅）\n回复序号即可添加"
                                )
                            )
                            return

                except Exception as e:
                    logger.error(f"Failed to process reply action: {e}")
                    await event.send(event.plain_result(f"❌ 操作失败: {e}"))
                    return

            try:
                await wait_for_reply(event)
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
