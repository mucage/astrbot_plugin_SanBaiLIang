from __future__ import annotations

import asyncio
import json
import math
import os
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.types import CallToolResult, TextContent

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_mucage"
PLUGIN_VERSION = "1.0.0"

STARTUP_TIMEOUT = 20
CALL_TIMEOUT = 30
CLOSE_TIMEOUT = 5

DENIED_MESSAGE = (
    "当前会话未授权使用账本指令。\n"
    "请在 AstrBot 插件配置的「允许使用指令的会话」中添加本会话。"
)


class MucageError(Exception):
    """账本服务相关错误。"""


class MucageToolError(MucageError):
    """工具本身返回的错误，消息保持原样。"""


class MucageServiceError(MucageError):
    """插件侧连接、启动或调用错误。"""


def _result_text(result: CallToolResult) -> str:
    """提取 MCP 工具返回的文本内容。"""
    parts: list[str] = []
    for block in result.content or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(str(text))
    return "\n".join(parts).strip()


class MucageMCPClient:
    """常驻单个 MCP stdio 子进程，串行复用同一会话。"""

    def __init__(self, command: str, script_path: str) -> None:
        raw_command = command.strip().strip('"').strip("'")
        self._command = os.path.expandvars(os.path.expanduser(raw_command))
        raw_path = script_path.strip().strip('"').strip("'")
        self._script_path = os.path.expandvars(os.path.expanduser(raw_path))
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[Any] | None = None
        self._ready: asyncio.Future[None] | None = None

    def _validate(self) -> None:
        if not self._command:
            raise MucageServiceError(
                "未配置 MCP 启动命令，请在插件配置中填写「MCP 启动命令」。"
            )
        if not self._script_path:
            raise MucageServiceError(
                "未配置 mucage_mcp.py 路径，请在插件配置中填写「mucage_mcp.py 路径」。"
            )
        if not os.path.isfile(self._script_path):
            raise MucageServiceError(f"找不到 mucage_mcp.py：{self._script_path}")

    async def call_json(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用工具并解析 JSON；工具报错时抛出保留原文的异常。"""
        result = await self._call(name, arguments)
        text = _result_text(result)
        if result.isError:
            raise MucageToolError(text or "记账工具返回了错误。")
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise MucageToolError(text or "记账工具返回了空响应。") from exc
        if not isinstance(payload, dict):
            raise MucageToolError(text)
        return payload

    async def _call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        self._validate()
        await self._ensure_started()

        queue = self._queue
        if queue is None:
            raise MucageServiceError("记账服务未就绪。")

        future: asyncio.Future[CallToolResult] = (
            asyncio.get_running_loop().create_future()
        )
        await queue.put((name, arguments, future))
        try:
            return await asyncio.wait_for(future, timeout=CALL_TIMEOUT)
        except TimeoutError as exc:
            await self.close()
            raise MucageServiceError(
                f"记账服务响应超时（{CALL_TIMEOUT} 秒）。"
            ) from exc

    async def _ensure_started(self) -> None:
        task = self._task
        if task is not None and not task.done():
            ready = self._ready
            if ready is None:
                raise MucageServiceError("记账服务未就绪。")
            await self._wait_ready(ready)
            return

        self._queue = asyncio.Queue()
        self._ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(), name="mucage-mcp")
        await self._wait_ready(self._ready)

    async def _wait_ready(self, ready: asyncio.Future[None]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=STARTUP_TIMEOUT)
        except TimeoutError as exc:
            await self.close()
            raise MucageServiceError(
                f"启动记账 MCP 服务超时（{STARTUP_TIMEOUT} 秒）。"
            ) from exc
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise MucageServiceError("记账服务已关闭。") from exc

    def _environment(self) -> dict[str, str]:
        """沿用 SDK 的安全默认环境，并补上账本需要的两个变量。"""
        env = get_default_environment()
        for key in ("MUCAGE_LEDGER_HOME", "MUCAGE_LEDGER_EXE"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    async def _run(self) -> None:
        """在独立任务中打开并关闭 MCP 会话，避免跨任务关闭 stdio。"""
        queue = self._queue
        ready = self._ready
        if queue is None or ready is None:
            return

        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=self._command,
                args=[self._script_path],
                env=self._environment(),
            )
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            if not ready.done():
                ready.set_result(None)

            while True:
                item = await queue.get()
                if item is None:
                    break
                name, arguments, future = item
                if future.done():
                    continue
                try:
                    result = await session.call_tool(name, arguments)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(
                            MucageServiceError(
                                f"调用 MCP 工具 {name} 失败：{exc}"
                            )
                        )
                    break
                if not future.done():
                    future.set_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = MucageServiceError(f"启动或连接记账 MCP 服务失败：{exc}")
            if not ready.done():
                ready.set_exception(error)
            self._fail_pending(error)
            logger.exception("Mucage MCP 连接异常")
        finally:
            self._fail_pending(MucageServiceError("记账 MCP 服务已断开。"))
            try:
                await stack.aclose()
            except BaseException:
                pass

    def _fail_pending(self, error: MucageServiceError) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                continue
            _, _, future = item
            if not future.done():
                future.set_exception(error)

    async def close(self) -> None:
        """停止子进程；在插件卸载、重载或超时后调用。"""
        task = self._task
        queue = self._queue
        ready = self._ready
        if task is None:
            return

        if queue is not None:
            try:
                queue.put_nowait(None)
            except Exception:
                pass

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=CLOSE_TIMEOUT)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception:
            pass
        finally:
            if ready is not None and not ready.done():
                ready.cancel()
            self._task = None
            self._queue = None
            self._ready = None


def _money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    formatted = f"{abs(number):,.2f}"
    return f"-¥{formatted}" if number < 0 else f"¥{formatted}"


def _format_balance(payload: dict[str, Any]) -> str:
    initialized = bool(payload.get("initialized"))
    suffix = "" if initialized else "（尚未初始化）"
    count = payload.get("recordCount", 0)
    return f"当前余额：{_money(payload.get('balance', 0))}{suffix}\n账目：{count} 笔"


def _format_record(payload: dict[str, Any]) -> str:
    record = payload.get("record") or {}
    category = record.get("category") or "其他"
    note = record.get("note") or ""
    detail = f"已记一笔：支出 {_money(record.get('amount', 0))} · {category}"
    if note:
        detail += f" · {note}"
    lines = [detail]
    if "balance" in payload:
        lines.append(f"当前余额：{_money(payload['balance'])}")
    return "\n".join(lines)


def _format_stats(payload: dict[str, Any]) -> str:
    range_info = payload.get("range") or {}
    month = str(range_info.get("start", ""))[:7]
    if not month:
        month = datetime.now().strftime("%Y-%m")

    expense = payload.get("expense") or {}
    try:
        count = int(expense.get("count", 0) or 0)
    except (TypeError, ValueError):
        count = 0

    if count == 0:
        return f"{month} 本月暂无支出"

    lines = [f"{month} 本月支出：{_money(expense.get('amount', 0))}（{count} 笔）"]
    categories = payload.get("byCategory") or []
    if categories:
        parts: list[str] = []
        for item in categories[:3]:
            if not isinstance(item, dict):
                continue
            try:
                share = float(item.get("share", 0) or 0) * 100
            except (TypeError, ValueError):
                share = 0.0
            parts.append(
                f"{item.get('category', '其他')} {_money(item.get('amount', 0))}"
                f" {share:.1f}%"
            )
        if parts:
            line = "分类：" + "；".join(parts)
            if len(categories) > 3:
                line += f"；等 {len(categories)} 类"
            lines.append(line)
    return "\n".join(lines)


@register(
    PLUGIN_NAME,
    "Mucage",
    "通过本机 MCP 服务提供固定记账指令：/余额、/记一笔、/本月支出。",
    PLUGIN_VERSION,
)
class MucagePlugin(Star):
    """通过 MCP stdio 读写「此地无银三百两」账本。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

        command = self.config.get("mcp_command") or "python"
        script_path = self.config.get("mcp_script_path") or ""
        self._mcp = MucageMCPClient(str(command), str(script_path))

    def _session_allowed(self, event: AstrMessageEvent) -> bool:
        sessions = self.config.get("allowed_sessions", [])
        if isinstance(sessions, str):
            sessions = [sessions]
        if not isinstance(sessions, (list, tuple, set)):
            return False

        current = (event.unified_msg_origin or "").strip()
        if not current:
            return False
        return any(
            str(item).strip() == current
            for item in sessions
            if str(item).strip()
        )

    def _error_text(self, exc: Exception) -> str:
        if isinstance(exc, MucageToolError):
            return str(exc)
        if isinstance(exc, MucageServiceError):
            return f"记账服务不可用：{exc}"
        logger.exception("调用记账 MCP 工具时出现未预期错误")
        return f"记账服务调用失败：{exc}"

    @filter.command("余额")
    async def balance(self, event: AstrMessageEvent):
        """查询当前总余额"""
        if not self._session_allowed(event):
            event.stop_event()
            yield event.plain_result(DENIED_MESSAGE)
            return

        try:
            payload = await self._mcp.call_json("get_balance", {})
        except Exception as exc:
            event.stop_event()
            yield event.plain_result(self._error_text(exc))
            return

        event.stop_event()
        yield event.plain_result(_format_balance(payload))

    @filter.command("记一笔")
    async def add_record(self, event: AstrMessageEvent):
        """记一笔支出：/记一笔 金额 [分类] [备注]"""
        if not self._session_allowed(event):
            event.stop_event()
            yield event.plain_result(DENIED_MESSAGE)
            return

        args = self._command_args(event, "记一笔")
        parts = args.split(maxsplit=2)
        if not parts:
            event.stop_event()
            yield event.plain_result("用法：/记一笔 金额 [分类] [备注]")
            return

        try:
            amount = float(parts[0])
        except ValueError:
            amount = 0.0
        if amount <= 0 or not math.isfinite(amount):
            event.stop_event()
            yield event.plain_result("金额不正确，示例：/记一笔 12.5 吃 午饭")
            return

        arguments: dict[str, Any] = {"amount": amount, "type": "支出"}
        if len(parts) > 1:
            arguments["category"] = parts[1]
        if len(parts) > 2:
            arguments["note"] = parts[2]

        try:
            payload = await self._mcp.call_json("add_record", arguments)
        except Exception as exc:
            event.stop_event()
            yield event.plain_result(self._error_text(exc))
            return

        event.stop_event()
        yield event.plain_result(_format_record(payload))

    @filter.command("本月支出")
    async def monthly_expense(self, event: AstrMessageEvent):
        """查询本月支出统计"""
        if not self._session_allowed(event):
            event.stop_event()
            yield event.plain_result(DENIED_MESSAGE)
            return

        month = datetime.now().strftime("%Y-%m")
        try:
            payload = await self._mcp.call_json("get_stats", {"month": month})
        except Exception as exc:
            event.stop_event()
            yield event.plain_result(self._error_text(exc))
            return

        event.stop_event()
        yield event.plain_result(_format_stats(payload))

    @staticmethod
    def _command_args(event: AstrMessageEvent, command: str) -> str:
        text = (event.message_str or "").strip()
        index = text.find(command)
        if index < 0:
            return ""
        return text[index + len(command) :].strip()

    async def terminate(self):
        """插件卸载或重载时关闭 MCP 子进程。"""
        await self._mcp.close()
