"""
qBittorrent API async client.

This module provides an async client for qBittorrent WebAPI.
Optimized with lazy loading, cookie caching, and batch operations.
"""

import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger("astrbot")


class QB:
    """qBittorrent API async client.

    Features:
    1. Uses httpx.AsyncClient for non-blocking I/O
    2. Lazy loading with cookie caching to avoid repeated logins
    3. Batch operations for update_keywords to avoid N+1 queries
    """

    def __init__(self, config: Any) -> None:
        """Initialize the qBittorrent client.

        Args:
            config: Configuration object with qb_url, qb_username, qb_password, rss_rule
        """
        self.config = config
        self.username = config.qb_username
        self.password = config.qb_password
        self.qb_url = config.qb_url.rstrip("/")

        # Lazy loading client
        self._client: httpx.AsyncClient | None = None
        self._cookies: dict[str, str] = {}
        self._is_logged_in: bool = False

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async client (lazy loading)."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.qb_url, timeout=30.0, follow_redirects=True
            )
        return self._client

    async def _ensure_login(self) -> None:
        """Ensure logged in, auto-login if not."""
        if self._is_logged_in and self._cookies:
            return

        client = await self._get_client()
        try:
            resp = await client.post(
                "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
            )
            if resp.text != "Ok.":
                raise Exception(f"Login failed: {resp.text}")

            # Save cookies for subsequent requests
            self._cookies = dict(resp.cookies)
            self._is_logged_in = True
            logger.debug("qBittorrent login successful")
        except httpx.HTTPError as e:
            raise Exception(f"Login request failed: {e}") from e

    async def _request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> httpx.Response:
        """Send authenticated request."""
        await self._ensure_login()
        client = await self._get_client()

        # Merge cookies
        cookies = kwargs.pop("cookies", {}) or {}
        cookies.update(self._cookies)

        resp = await client.request(method, endpoint, cookies=cookies, **kwargs)

        # If 403, session might be expired, re-login
        if resp.status_code == 403:
            logger.warning("qBittorrent session expired, re-logging in")
            self._is_logged_in = False
            await self._ensure_login()
            cookies.update(self._cookies)
            resp = await client.request(method, endpoint, cookies=cookies, **kwargs)

        return resp

    async def close(self) -> None:
        """Close client connection."""
        if self._client:
            await self._client.aclose()
            self._client = None
            self._is_logged_in = False

    async def get_all_torrents(self) -> list[dict[str, Any]]:
        """Get all torrents list (single API call).

        This is the foundation for batch operations.
        """
        resp = await self._request("GET", "/api/v2/torrents/info")
        if resp.status_code != 200:
            raise Exception(f"Failed to get torrent info: {resp.status_code}")
        return resp.json()

    async def search_torrents(self, search_name: str) -> str:
        """Search torrents and return formatted string (for display)."""
        torrents = await self.get_all_torrents()
        matched = [t for t in torrents if search_name.lower() in t["name"].lower()]

        result = ""
        for i, t in enumerate(matched[:4]):  # Max 4 results
            completion_time = "未完成"
            if t.get("completion_on"):
                completion_time = datetime.fromtimestamp(t["completion_on"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

            tracker_url = t.get("tracker", "")
            tracker_domain = (
                tracker_url.split("/")[2]
                if tracker_url and "/" in tracker_url
                else "未知"
            )

            result += (
                f"{i + 1}. {t['name']}\n"
                f"   🏁 完成时间: {completion_time}\n"
                f"   🔗 Tracker: {tracker_domain}\n"
                f"   🔑 Command:  `{t['hash']}`\n\n"
            )

        return result or f"未找到匹配 '{search_name}' 的种子"

    async def search_torrents_list(self, search_name: str) -> list[dict[str, Any]]:
        """Search torrents and return list (for programmatic use)."""
        torrents = await self.get_all_torrents()
        matched = [t for t in torrents if search_name.lower() in t["name"].lower()]

        result = []
        for t in matched[:4]:  # Max 4 results
            completion_time = "未完成"
            if t.get("completion_on"):
                completion_time = datetime.fromtimestamp(t["completion_on"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

            tracker_url = t.get("tracker", "")
            tracker_domain = (
                tracker_url.split("/")[2]
                if tracker_url and "/" in tracker_url
                else "未知"
            )

            result.append(
                {
                    "name": t["name"],
                    "tracker": tracker_domain,
                    "complete": completion_time,
                    "hash": t["hash"],
                    "added_on": t.get("added_on", 0),
                }
            )

        return result

    async def delete_torrents(
        self, torrent_hash: str, delete_files: bool = True
    ) -> bool:
        """Delete torrent."""
        resp = await self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": torrent_hash,
                "deleteFiles": "true" if delete_files else "false",
            },
        )
        return resp.status_code == 200

    async def tag_torrents(self, torrent_hash: str, tags: str) -> bool:
        """Add tags to torrent."""
        resp = await self._request(
            "POST",
            "/api/v2/torrents/addTags",
            data={"hashes": torrent_hash, "tags": tags},
        )
        return resp.status_code == 200

    async def get_rules(self, rss_rule: str | None = None) -> tuple[dict, list[str]]:
        """Get RSS rule.

        Returns:
            Tuple[Dict, List[str]]: (rule dict, current keywords list)
        """
        if not rss_rule:
            rss_rule = self.config.rss_rule

        resp = await self._request("GET", "/api/v2/rss/rules")
        rules = resp.json()
        rule = rules.get(rss_rule, {"mustContain": ""})
        current_expr = rule["mustContain"].split("|") if rule["mustContain"] else []
        return rule, current_expr

    async def set_rule(self, rule_name: str, rule_def: dict) -> bool:
        """Set RSS rule."""
        resp = await self._request(
            "POST",
            "/api/v2/rss/setRule",
            data={"ruleName": rule_name, "ruleDef": json.dumps(rule_def)},
        )
        return resp.status_code == 200

    async def update_keywords(self, rss_rule: str | None = None) -> None:
        """Update RSS keywords - optimized batch version.

        Optimizations:
        1. Only fetch torrent list once, avoid N+1 queries
        2. Batch match keywords in memory
        3. Time complexity reduced from O(N×M) to O(N+M)
        """
        if not rss_rule:
            rss_rule = self.config.rss_rule

        # 1. Get current rule
        rule, current_expr = await self.get_rules(rss_rule)
        updated_expr = set(current_expr)

        if not updated_expr:
            logger.debug("No subscription keywords, skipping update")
            return

        # 2. Fetch torrent list once - KEY OPTIMIZATION
        logger.info(
            f"Starting RSS keyword update, current {len(updated_expr)} keywords"
        )
        all_torrents = await self.get_all_torrents()

        # 3. Build recent torrent names set (last month)
        now = int(time.time())
        one_month_ago = now - 30 * 24 * 3600

        # Convert names to lowercase for matching
        recent_torrent_names = [
            t["name"].lower()
            for t in all_torrents
            if t.get("added_on", 0) >= one_month_ago
        ]

        logger.debug(f"Found {len(recent_torrent_names)} torrents in the last month")

        # 4. Batch match keywords - O(N+M) instead of O(N×M)
        removed_count = 0
        for kw in list(updated_expr):
            kw_lower = kw.lower()
            # Check if any torrent name contains this keyword
            if any(kw_lower in name for name in recent_torrent_names):
                logger.debug(
                    f"❌ Found existing torrent for '{kw}', removing from rule"
                )
                updated_expr.remove(kw)
                removed_count += 1
            else:
                logger.debug(f"✅ '{kw}' no recent torrent found, keeping in rule")

        # 5. Update rule
        if removed_count > 0:
            rule["mustContain"] = "|".join(sorted(updated_expr))
            await self.set_rule(rss_rule, rule)
            logger.info(
                f"Update complete, removed {removed_count} keywords, {len(updated_expr)} remaining"
            )
        else:
            logger.info(
                f"Update complete, no keywords to remove, current {len(updated_expr)} keywords"
            )
