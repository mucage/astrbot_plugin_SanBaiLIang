# Copyright (C) 2026 mucage
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.types import CallToolResult, TextContent

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.util import SessionController, session_waiter

PLUGIN_NAME = "astrbot_plugin_SanBaiLIang"
PLUGIN_VERSION = "1.2.0"

STARTUP_TIMEOUT = 20
CALL_TIMEOUT = 30
CLOSE_TIMEOUT = 5

IMPORT_TIMEOUT_SECONDS = 120
IMPORT_TIMEOUT_MESSAGE = "导入失败：未能接收到文件。"
FILE_RETENTION_SECONDS = 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
MAX_IMPORT_BYTES = 20 * 1024 * 1024

DENIED_MESSAGE = (
    "当前会话未授权使用账本指令。\n"
    "请在 AstrBot 插件配置的「允许使用指令的会话」中添加本会话。"
)

AUTO_SAVE_USAGE = (
    "用法：/自动攒着 [关闭 | 每周|每月|每年 金额 [计入|不计入]]"
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


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _auto_save_line(auto: Any) -> str:
    if not isinstance(auto, dict) or not auto.get("enabled"):
        return ""
    period = auto.get("periodLabel") or auto.get("period") or ""
    count_text = "计入支出" if auto.get("countAsExpense") else "不计入支出"
    line = f"自动攒着：{period} {_money(auto.get('amount', 0))}（{count_text}）"
    next_at = auto.get("nextAt")
    if next_at:
        line += f"，下次 {next_at}"
    return line


def _format_balance(payload: dict[str, Any]) -> str:
    initialized = bool(payload.get("initialized"))
    suffix = "" if initialized else "（尚未初始化）"
    total = payload.get("total", payload.get("balance", 0))
    lines = [f"总余额：{_money(total)}{suffix}"]
    if "saved" in payload or "available" in payload:
        lines.append(f"攒着：{_money(payload.get('saved', 0))}")
        lines.append(f"可用：{_money(payload.get('available', total))}")
    lines.append(f"账目：{payload.get('recordCount', 0)} 笔")

    auto_line = _auto_save_line(payload.get("autoSave"))
    if auto_line:
        lines.append(auto_line)
    return "\n".join(lines)


def _format_savings_change(payload: dict[str, Any], kind: str) -> str:
    value_key = "saved" if kind == "攒着" else "available"
    other_key = "available" if kind == "攒着" else "saved"
    other_label = "可用" if kind == "攒着" else "攒着"
    total = payload.get("total", payload.get("balance", 0))
    line = f"总余额：{_money(total)}"
    if other_key in payload:
        line += f"，{other_label}：{_money(payload[other_key])}"
    return f"{kind}已调整为：{_money(payload.get(value_key, 0))}\n{line}"


def _format_auto_query(auto: Any) -> str:
    if not isinstance(auto, dict) or not auto.get("enabled"):
        return "自动攒着：未开启"
    period = auto.get("periodLabel") or auto.get("period") or ""
    count_text = "计入支出" if auto.get("countAsExpense") else "不计入支出"
    lines = [f"自动攒着：{period} {_money(auto.get('amount', 0))}（{count_text}）"]
    if auto.get("lastAt"):
        lines.append(f"上次转入：{auto['lastAt']}")
    if auto.get("nextAt"):
        lines.append(f"下次转入：{auto['nextAt']}")
    return "\n".join(lines)


def _format_auto_change(payload: dict[str, Any]) -> str:
    if not payload.get("enabled"):
        return "自动攒着已关闭"
    period = payload.get("periodLabel") or payload.get("period") or ""
    count_text = "计入支出" if payload.get("countAsExpense") else "不计入支出"
    lines = [f"自动攒着已更新：{period} {_money(payload.get('amount', 0))}（{count_text}）"]
    if payload.get("nextAt"):
        lines.append(f"下次转入：{payload['nextAt']}")
    return "\n".join(lines)


def _format_record(payload: dict[str, Any]) -> str:
    record = payload.get("record") or {}
    category = record.get("category") or "其他"
    note = record.get("note") or ""
    detail = f"已记一笔：支出 {_money(record.get('amount', 0))} · {category}"
    if note:
        detail += f" · {note}"
    lines = [detail]
    if "balance" in payload:
        lines.append(f"当前总余额：{_money(payload['balance'])}")
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


def _format_import(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or ""
    record_count = payload.get("recordCount", 0)
    balance = payload.get("balance")
    lines = [f"导入完成：{summary}" if summary else "导入完成。"]
    current = f"本机现有 {record_count} 笔账目"
    if balance is not None:
        current += f"，总余额 {_money(balance)}"
    lines.append(current)

    try:
        local_only = int(payload.get("localOnly", 0) or 0)
    except (TypeError, ValueError):
        local_only = 0
    if local_only > 0:
        lines.append(f"本机另有 {local_only} 笔账目不在导入文件中。")
    return "\n".join(lines)


def _format_export(payload: dict[str, Any], filename: str, size: int) -> str:
    record_count = payload.get("recordCount", 0)
    balance = payload.get("balance")
    lines = [f"导出完成：{record_count} 笔账目"]
    if balance is not None:
        lines[0] += f"，总余额 {_money(balance)}"
    lines.append(f"文件：{filename}（{_human_size(size)}）")
    return "\n".join(lines)


def _is_ledger_file(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except (OSError, UnicodeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("format") == "mucage-ledger"


@register(
    PLUGIN_NAME,
    "Mucage",
    "通过本机 MCP 服务提供固定记账指令，支持余额拆分、自动攒着和导入/导出。",
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

        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.import_dir = self.data_dir / "imports"
        self.export_dir = self.data_dir / "exports"
        self.import_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        self._cleanup_files()
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="mucage-file-cleanup",
        )

    async def terminate(self) -> None:
        task = self._cleanup_task
        self._cleanup_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        await self._mcp.close()

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

    def _error_text(self, exc: Exception, prefix: str = "记账服务不可用") -> str:
        if isinstance(exc, MucageToolError):
            return str(exc)
        if isinstance(exc, MucageServiceError):
            return f"{prefix}：{exc}"
        logger.exception("调用记账 MCP 工具时出现未预期错误")
        return f"{prefix}：{exc}"

    def _new_file_path(self, directory: Path, prefix: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return directory / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}.json"

    def _cleanup_files(self) -> None:
        cutoff = time.time() - FILE_RETENTION_SECONDS
        for directory in (self.import_dir, self.export_dir):
            try:
                entries = list(directory.iterdir())
            except OSError:
                logger.warning("无法扫描账本文件目录：%s", directory)
                continue
            for path in entries:
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        logger.debug("已清理过期账本文件：%s", path)
                except OSError:
                    logger.warning("清理账本文件失败：%s", path)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            self._cleanup_files()

    async def _import_file(self, file_component: File) -> str:
        self._cleanup_files()

        try:
            source = await file_component.get_file()
        except Exception as exc:
            raise MucageServiceError(f"无法下载文件：{exc}") from exc
        if not source:
            raise MucageServiceError("未能读取文件内容。")

        source_path = Path(source)
        try:
            size = source_path.stat().st_size
        except OSError as exc:
            raise MucageServiceError(f"无法读取文件：{exc}") from exc
        if size <= 0:
            raise MucageServiceError("文件为空。")
        if size > MAX_IMPORT_BYTES:
            raise MucageServiceError(
                f"文件过大，最大支持 {_human_size(MAX_IMPORT_BYTES)}。"
            )
        if not _is_ledger_file(source_path):
            raise MucageServiceError("文件不是有效的账本同步文件。")

        target = self._new_file_path(self.import_dir, "import")
        try:
            shutil.copyfile(source_path, target)
        except OSError as exc:
            raise MucageServiceError(f"保存导入文件失败：{exc}") from exc

        payload = await self._mcp.call_json("import_ledger", {"path": str(target)})
        return _format_import(payload)

    async def _export_file(self) -> tuple[str, Path]:
        self._cleanup_files()
        target = self._new_file_path(self.export_dir, "export")
        payload = await self._mcp.call_json("export_ledger", {"path": str(target)})
        if not target.is_file():
            raise MucageServiceError("导出完成但未找到导出文件。")
        return _format_export(payload, target.name, target.stat().st_size), target

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

    @filter.command("攒着")
    async def savings(self, event: AstrMessageEvent):
        """调整攒着金额：/攒着 金额"""
        if not self._session_allowed(event):
            event.stop_event()
            yield event.plain_result(DENIED_MESSAGE)
            return

        amount = self._amount_arg(self._command_args(event, "攒着"))
        if amount is None:
            event.stop_event()
            yield event.plain_result("用法：/攒着 金额（金额不能为负）")
            return

        try:
            payload = await self._mcp.call_json("set_savings", {"amount": amount})
        except Exception as exc:
            event.stop_event()
            yield event.plain_result(self._error_text(exc, "攒着失败"))
            return

        event.stop_event()
        yield event.plain_result(_format_savings_change(payload, "攒着"))

    @filter.command("可用")
    async def available(self, event: AstrMessageEvent):
        """调整可用金额：/可用 金额"""
        if not self._session_allowed(event):
            event.stop_event()
            yield event.plain_result(DENIED_MESSAGE)
            return

        amount = self._amount_arg(self._command_args(event, "可用"))
        if amount is None:
            event.stop_event()
            yield event.plain_result("用法：/可用 金额（金额不能为负）")
            return

        try:
            payload = await self._mcp.call_json("set_available", {"amount": amount})
        except Exception as exc:
            event.stop_event()
            yield event.plain_result(self._error_text(exc, "可用失败"))
            return

        event.stop_event()
        yield event.plain_result(_format_savings_change(payload, "可用"))

    @filter.command("自动攒着")
    async def auto_savings(self, event: AstrMessageEvent):
        """查看或配置自动攒着"""
        if not self._session_allowed(event):
            event.stop_event()
            yield event.plain_result(DENIED_MESSAGE)
            return

        args = self._command_args(event, "自动攒着")
        if not args:
            try:
                payload = await self._mcp.call_json("get_balance", {})
            except Exception as exc:
                event.stop_event()
                yield event.plain_result(self._error_text(exc))
                return

            event.stop_event()
            yield event.plain_result(_format_auto_query(payload.get("autoSave")))
            return

        parts = args.split()
        action = parts[0].lower()
        if action in {"关闭", "关", "off", "停用"}:
            if len(parts) != 1:
                event.stop_event()
                yield event.plain_result(AUTO_SAVE_USAGE)
                return
            arguments = {"enabled": False}
        else:
            if action in {"开启", "开", "on", "启用"}:
                parts = parts[1:]
            if len(parts) < 2:
                event.stop_event()
                yield event.plain_result(AUTO_SAVE_USAGE)
                return

            periods = {
                "每周": "每周",
                "weekly": "每周",
                "每月": "每月",
                "monthly": "每月",
                "每年": "每年",
                "yearly": "每年",
            }
            period = periods.get(parts[0].lower())
            amount = self._amount_arg(parts[1], require_positive=True)
            if period is None:
                event.stop_event()
                yield event.plain_result(AUTO_SAVE_USAGE)
                return
            if amount is None:
                event.stop_event()
                yield event.plain_result(
                    "金额不正确，示例：/自动攒着 每月 200 计入"
                )
                return

            arguments = {
                "enabled": True,
                "period": period,
                "amount": amount,
            }
            if len(parts) >= 3:
                if len(parts) > 3:
                    event.stop_event()
                    yield event.plain_result(AUTO_SAVE_USAGE)
                    return
                flag = parts[2].lower()
                if flag in {"计入", "计入支出", "是", "true", "1", "yes"}:
                    arguments["countAsExpense"] = True
                elif flag in {"不计入", "不计入支出", "否", "false", "0", "no"}:
                    arguments["countAsExpense"] = False
                else:
                    event.stop_event()
                    yield event.plain_result(AUTO_SAVE_USAGE)
                    return

        try:
            payload = await self._mcp.call_json("set_savings_auto", arguments)
        except Exception as exc:
            event.stop_event()
            yield event.plain_result(self._error_text(exc, "自动攒着失败"))
            return

        event.stop_event()
        yield event.plain_result(_format_auto_change(payload))

    @filter.command("导入")
    async def import_ledger(self, event: AstrMessageEvent):
        """导入账本同步文件；等待文件最长 2 分钟"""
        if not self._session_allowed(event):
            event.stop_event()
            yield event.plain_result(DENIED_MESSAGE)
            return

        event.stop_event()
        await event.send(
            event.plain_result("请在 2 分钟内发送账本同步文件（mucage-ledger JSON）。")
        )

        @session_waiter(timeout=IMPORT_TIMEOUT_SECONDS, record_history_chains=False)
        async def wait_for_file(
            controller: SessionController,
            file_event: AstrMessageEvent,
        ) -> None:
            file_component = next(
                (
                    component
                    for component in file_event.get_messages()
                    if isinstance(component, File)
                ),
                None,
            )
            if file_component is None:
                return

            # 文件已收到，立即停止计时，后续只执行一次导入。
            controller.stop()
            try:
                reply = await self._import_file(file_component)
            except MucageToolError as exc:
                await file_event.send(file_event.plain_result(str(exc)))
            except Exception as exc:
                await file_event.send(
                    file_event.plain_result(self._error_text(exc, "导入失败"))
                )
            else:
                await file_event.send(file_event.plain_result(reply))

        try:
            await wait_for_file(event)
        except TimeoutError:
            await event.send(event.plain_result(IMPORT_TIMEOUT_MESSAGE))
        except Exception as exc:
            logger.exception("等待导入文件失败")
            await event.send(
                event.plain_result(self._error_text(exc, "导入失败"))
            )

    @filter.command("导出")
    async def export_ledger(self, event: AstrMessageEvent) -> None:
        """导出完整账本并发送同步文件"""
        if not self._session_allowed(event):
            event.stop_event()
            await event.send(event.plain_result(DENIED_MESSAGE))
            return

        try:
            reply, path = await self._export_file()
        except Exception as exc:
            event.stop_event()
            await event.send(
                event.plain_result(self._error_text(exc, "导出失败"))
            )
            return

        event.stop_event()
        await event.send(event.plain_result(reply))
        try:
            await event.send(
                event.chain_result([File(name=path.name, file=str(path))])
            )
        except Exception as exc:
            logger.exception("发送导出文件失败")
            await event.send(
                event.plain_result(f"导出文件发送失败：{exc}")
            )

    @staticmethod
    def _amount_arg(text: str, require_positive: bool = False) -> float | None:
        try:
            amount = float(text)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(amount) or amount < 0:
            return None
        if require_positive and amount <= 0:
            return None
        return amount

    @staticmethod
    def _command_args(event: AstrMessageEvent, command: str) -> str:
        text = (event.message_str or "").strip()
        index = text.find(command)
        if index < 0:
            return ""
        return text[index + len(command) :].strip()
