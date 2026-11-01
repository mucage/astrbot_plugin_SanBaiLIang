# astrbot_plugin_mucage

为 AstrBot 提供固定指令，读写本机「此地无银三百两」账本。

插件不直接读写 `ledger.json`，也不重复实现余额、分类合并等逻辑，而是通过 Python MCP 客户端 SDK
以 stdio 启动账本软件自带的 `mucage_mcp.py`，复用其 11 个 MCP 工具中的 `get_balance`、
`add_record`、`get_stats`。

> AstrBot 已原生支持 MCP，本插件不是接入账本的必需项，只用于提供确定性的固定指令。

## 指令

| 指令 | 说明 | 调用工具 |
| --- | --- | --- |
| `/余额` | 查询当前总余额与账目数量 | `get_balance` |
| `/记一笔 金额 [分类] [备注]` | 记一笔支出，备注可包含空格 | `add_record` |
| `/本月支出` | 查询本月支出合计与分类占比 | `get_stats` |

示例：

```text
/记一笔 12.5 吃 午饭
```

分类省略时由账本归入「其他」。工具返回错误时，插件原样显示错误文本。

## 前置条件

- AstrBot `>= 4.16,<5`。
- 本机已安装「此地无银三百两」账本软件，且存在 MCP 启动包装器 `mucage_mcp.py`。
- `MucageLedger.exe` 与 `mucage_mcp.py` 在同一目录；如果不在同一目录，请在启动 AstrBot 前设置
  环境变量 `MUCAGE_LEDGER_EXE` 指向 `MucageLedger.exe`。
- AstrBot 运行环境中的 Python 可以运行该包装器。插件依赖 `mcp` SDK，见 `requirements.txt`。

本插件不内置账本软件，也不包含任何写死的本机路径，所有路径由使用者自行配置。

## 安装

1. 将整个 `astrbot_plugin_mucage` 目录放入 AstrBot 的 `data/plugins/`：
   - 源码部署：`AstrBot/data/plugins/astrbot_plugin_mucage`
   - AstrBot Desktop：`%USERPROFILE%\.astrbot\data\plugins\astrbot_plugin_mucage`
2. 安装依赖。AstrBot 通常会自动读取 `requirements.txt`；也可以在 AstrBot 使用的 Python 环境中执行：

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. 在 WebUI 的「插件」页刷新/重载插件，确认 `此地无银三百两账本` 加载成功。
4. 打开插件配置，填写下文的配置项。

## 配置

配置项定义在 `_conf_schema.json`，可在 WebUI 插件配置页直接编辑。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `mcp_command` | `python` | MCP 启动命令。若 AstrBot 环境找不到 `python`，填写该环境中 Python 解释器的绝对路径 |
| `mcp_script_path` | 空 | `mucage_mcp.py` 的完整路径，必须填写 |
| `allowed_sessions` | 空列表 | 允许使用指令的会话白名单（UMO） |

示例（仅示意，请替换为你自己的路径）：

```json
{
  "mcp_command": "python",
  "mcp_script_path": "D:\\YourApps\\MucageLedger\\mucage_mcp.py",
  "allowed_sessions": ["webchat:FriendMessage:your-session-id"]
}
```

### 会话白名单

只有 `allowed_sessions` 中的会话可以使用上述指令；其他会话会收到未授权提示。留空表示所有会话均不可使用。

- 在 WebUI 配置页点击「允许使用指令的会话」，选择目标会话；或
- 在目标会话发送 `/sid`，把返回的 `UMO` 加入 `allowed_sessions`。

白名单按完整 UMO 精确匹配，不支持通配符；同一会话内的所有成员都视为已授权。

## 重要：不要同时启用 AstrBot 的 MCP 服务器

启用本插件后，请到 WebUI 的「插件 → MCP」中**停用或删除** `mucage-ledger` 服务器。

原因：本插件会自行启动一个 `mucage_mcp.py` 子进程。如果 AstrBot 的 MCP 服务器同时连接同一个账本，
就会出现两个 MCP 子进程同时写账本，可能造成冲突或非预期结果。

可以用下面的命令检查是否还有残留进程（Windows）：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'mucage_mcp.py' } |
  Select-Object ProcessId, CommandLine
```

## 启用

1. 确认已填写 `mcp_script_path`，且 `allowed_sessions` 包含测试会话。
2. 在 WebUI 启用插件。
3. 在允许的会话中发送 `/余额` 验证连接。

首次调用时才启动 MCP 子进程；之后复用同一个会话。插件被停用、卸载或重载时会关闭该子进程。

## 测试（使用临时账本，不要动真实账本）

`MUCAGE_LEDGER_HOME` 可以让账本软件使用临时数据目录。插件会将它和 `MUCAGE_LEDGER_EXE` 显式传给
MCP 子进程（不会透传其他无关环境变量）。请先设置该环境变量，再从**同一个终端**启动 AstrBot，
使 AstrBot 进程能够读取到它。

```powershell
$env:MUCAGE_LEDGER_HOME = Join-Path $env:TEMP "mucage-plugin-test"
Remove-Item -Recurse -Force $env:MUCAGE_LEDGER_HOME -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $env:MUCAGE_LEDGER_HOME | Out-Null
```

如果 AstrBot 已在使用中，请完全退出后重新启动（Desktop 可从上面的终端启动）：

```powershell
& "$env:LOCALAPPDATA\AstrBot\astrbot-desktop-tauri.exe"
```

然后在已加入白名单的会话中依次发送：

1. `/余额`
   - 临时账本未初始化时，预期为 `当前余额：¥0.00（尚未初始化）`、`账目：0 笔`。
2. `/记一笔 12.5 吃 午饭`
   - 预期为 `已记一笔：支出 ¥12.50 · 吃 · 午饭`，并显示当前余额。
3. `/余额`
   - 预期余额比上一步减少 `12.50`。若初始为 0，则显示 `当前余额：-¥12.50`、`账目：1 笔`。
4. `/本月支出`
   - 预期显示本月支出 `¥12.50（1 笔）`，以及分类 `吃` 的占比。

验证隔离生效：

- `%TEMP%\mucage-plugin-test` 下应生成账本文件；
- `%APPDATA%\MucageLedger\ledger.json` 的修改时间不应变化。

测试结束后，移除 `MUCAGE_LEDGER_HOME` 环境变量并重启 AstrBot，即可恢复真实账本。

## 已知限制

- 仅提供支出记账，暂不支持收入、指定日期、分类管理等指令。
- 白名单按完整 UMO 精确匹配，不支持通配符；修改配置后建议重载插件。
- 每个插件实例只维护一个 MCP 子进程，所有指令串行执行。
- MCP 启动最长等待 20 秒，单次工具调用最长等待 30 秒；超时会关闭连接，下次调用重新启动。
- 需要账本软件及 `mucage_mcp.py` 已存在；插件不负责安装账本软件。
- `mcp_command` 必须能在 AstrBot 的运行环境中执行；Docker 部署时路径必须是容器内路径。
- 插件只提供固定指令，不注册 LLM 工具，不会让模型自行记账。
- 群聊白名单授权的是整个会话，无法区分会话内具体成员。
- 账本余额与合并逻辑由账本软件的 MCP 服务负责，插件只展示其返回值。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `未配置 mucage_mcp.py 路径` | 在插件配置中填写 `mcp_script_path` |
| `找不到 mucage_mcp.py` | 检查路径是否正确、文件是否存在 |
| `No module named mcp` | 在 AstrBot 运行环境中安装 `requirements.txt` |
| `记账服务不可用：...` | 检查 `mcp_command` 和脚本路径；可在终端手动运行 `python mucage_mcp.py` 验证 |
| 工具返回 `金额必须大于 0` | 这是账本工具的原样错误提示，修正金额后重试 |
| 指令无响应且提示未授权 | 将当前会话 UMO 加入 `allowed_sessions` |

## 卸载

1. 在 WebUI 停用插件（会触发 `terminate` 并关闭 MCP 子进程）。
2. 删除插件目录。
3. 如需彻底清理，删除插件配置 `data/config/astrbot_plugin_mucage_config.json`。

## 参考

- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot MCP 使用文档](https://docs.astrbot.app/use/mcp.html)
- 插件按官方 `Star` / `register` / `filter` 与 `_conf_schema.json` 配置机制实现。
