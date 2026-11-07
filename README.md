# astrbot_plugin_SanBaiLIang

为 AstrBot 提供固定指令，读写本机「此地无银三百两」账本。

插件不直接读写 `ledger.json`，也不重复实现余额、分类合并等逻辑，而是通过 Python MCP 客户端 SDK
以 stdio 启动账本软件自带的 `mucage_mcp.py`，复用其 MCP 工具。支持余额拆分（总余额 = 攒着 + 可用）、
自动攒着配置，以及通过聊天文件导入、导出完整账本。

> AstrBot 已原生支持 MCP，本插件不是接入账本的必需项，只用于提供确定性的固定指令。
## 本插件需要搭配https://github.com/mucage/SanBaiLiang 软件一起食用喵！支持無影谢谢喵！
## 指令

| 指令 | 说明 | 调用工具 |
| --- | --- | --- |
| `/余额` | 查询总余额、攒着、可用、账目数量与自动攒着状态 | `get_balance` |
| `/记一笔 金额 [分类] [备注]` | 记一笔支出，备注可包含空格 | `add_record` |
| `/本月支出` | 查询本月支出合计与分类占比 | `get_stats` |
| `/攒着 金额` | 调整攒着金额，总余额不变 | `set_savings` |
| `/可用 金额` | 调整可用金额，总余额不变 | `set_available` |
| `/自动攒着` | 查询自动攒着配置 | `get_balance` |
| `/自动攒着 关闭` | 关闭自动攒着 | `set_savings_auto` |
| `/自动攒着 每周\|每月\|每年 金额 [计入\|不计入]` | 开启/修改自动攒着，可指定是否视作支出 | `set_savings_auto` |
| `/导入` | 等待并导入账本同步文件，最长等待 2 分钟 | `import_ledger` |
| `/导出` | 导出完整账本，并把 JSON 文件发送到当前会话 | `export_ledger` |

示例：

```text
/记一笔 12.5 吃 午饭
/攒着 300
/可用 700
/自动攒着 每月 200 计入
```

分类省略时由账本归入「其他」。工具返回错误时，插件原样显示错误文本。

### 余额拆分与自动攒着

账本服务把总余额拆成「攒着」和「可用」两栏：

- `总余额 = 攒着 + 可用`。
- `/攒着` 和 `/可用` 只调整账面划分，不改变总余额，也不会真正转账。
- `/自动攒着` 按每周/每月/每年把可用的一部分转入攒着；可用不足时跳过该期，错过的周期由账本服务逐期补足。
- 自动攒着可选择「计入」或「不计入」支出；计入时会生成分类为「攒着」的支出记录，计入限额与统计。
- `/自动攒着` 不带参数时显示当前配置；`/自动攒着 关闭` 关闭自动攒着。

### 导入

发送 `/导入` 后，机器人会提示发送账本同步文件，并等待最多 2 分钟：

- 在收到文件前不会执行导入操作；普通文字消息会被忽略。
- 收到文件后立即停止计时，校验并保存文件，然后调用 `import_ledger` 合并。
- 2 分钟内没有收到文件时，返回：`导入失败：未能接收到文件。`
- 只接受账本软件导出的 `mucage-ledger` JSON 文件，最大 20 MB。
- 合并规则由账本服务负责：按 `syncId` 去重，`updatedAt` 较新者胜出，源文件不会被修改。

### 导出

`/导出` 调用 `export_ledger` 生成完整账本 JSON，并作为文件发送到当前会话。文件只包含账本同步
数据，不包含本插件配置、日志或本机路径。

### 文件存储与清理

导入、导出的文件按时间戳和随机后缀规范存放：

```text
data/plugin_data/astrbot_plugin_SanBaiLIang/
  imports/import-YYYYMMDD-HHMMSS-xxxxxxxx.json
  exports/export-YYYYMMDD-HHMMSS-xxxxxxxx.json
```

插件启动时清理一次，运行期间每 6 小时清理一次，删除超过 24 小时的文件。导出文件包含完整账本，
请在清理前妥善保管，避免把 `plugin_data` 目录或其中文件分享给他人。

## 前置条件

- AstrBot `>= 4.16,<5`。
- Windows 桌面端「此地无银三百两」账本软件 **10.27.3 或更高版本**（提供余额拆分、自动攒着、
  `import_ledger` / `export_ledger` 工具）。旧版 10.27.2 不支持攒着相关指令。
- 存在 MCP 启动包装器 `mucage_mcp.py`。
- `MucageLedger.exe` 与 `mucage_mcp.py` 在同一目录；如果不在同一目录，请在启动 AstrBot 前设置
  环境变量 `MUCAGE_LEDGER_EXE` 指向 `MucageLedger.exe`。
- AstrBot 运行环境中的 Python 可以运行该包装器。插件依赖 `mcp` SDK，见 `requirements.txt`。

本插件不内置账本软件，也不包含任何写死的本机路径，所有路径由使用者自行配置。

## 安装

1. 将整个 `astrbot_plugin_SanBaiLIang` 目录放入 AstrBot 的 `data/plugins/`：
   - 源码部署：`AstrBot/data/plugins/astrbot_plugin_SanBaiLIang`
   - AstrBot Desktop：`%USERPROFILE%\.astrbot\data\plugins\astrbot_plugin_SanBaiLIang`
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

白名单按完整 UMO 精确匹配，不支持通配符；同一会话内的所有成员都视为已授权。`/导入` 的等待状态
也绑定到发起指令的会话。

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

首次调用时才启动 MCP 子进程；之后复用同一个会话。插件被停用、卸载或重载时会关闭该子进程，
并停止文件清理任务。

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
   - 临时账本未初始化时，预期为 `总余额：¥0.00（尚未初始化）`、`攒着：¥0.00`、`可用：¥0.00`、`账目：0 笔`。
2. `/记一笔 12.5 吃 午饭`
   - 预期为 `已记一笔：支出 ¥12.50 · 吃 · 午饭`，并显示当前总余额。
3. `/余额`
   - 预期总余额比上一步减少 `12.50`。若初始为 0，则显示 `总余额：-¥12.50`、`账目：1 笔`。
4. `/本月支出`
   - 预期显示本月支出 `¥12.50（1 笔）`，以及分类 `吃` 的占比。
5. `/导出`
   - 预期收到一段导出摘要和一个 `export-*.json` 文件。
6. `/导入`
   - 不发送文件，等待 2 分钟，预期返回 `导入失败：未能接收到文件。`。
   - 再次发送 `/导入`，随后发送刚导出的 JSON 文件，预期返回合并摘要与当前总余额。

测试攒着/可用/自动攒着时，需要总余额为正。可先在桌面端打开临时账本、设置总余额（例如 1000），
关闭桌面端后再启动 AstrBot；也可以在 MCP 中调用 `set_balance`。然后发送：

1. `/攒着 300`
   - 预期为 `攒着已调整为：¥300.00`，总余额仍为 `¥1000.00`，可用变为 `¥700.00`。
2. `/可用 700`
   - 预期为 `可用已调整为：¥700.00`，总余额仍为 `¥1000.00`，攒着仍为 `¥300.00`。
3. `/余额`
   - 预期显示总余额、攒着、可用和账目数量。
4. `/自动攒着 每月 200 计入`
   - 预期显示自动攒着周期、金额、计入支出和下次转入时间。
5. `/自动攒着`
   - 预期显示当前自动攒着配置。
6. `/自动攒着 关闭`
   - 预期返回 `自动攒着已关闭`。

验证隔离生效：

- `%TEMP%\mucage-plugin-test` 下应生成账本文件；
- `%APPDATA%\MucageLedger\ledger.json` 的修改时间不应变化；
- 导入导出文件位于 `%USERPROFILE%\.astrbot\data\plugin_data\astrbot_plugin_SanBaiLIang`。

测试结束后，移除 `MUCAGE_LEDGER_HOME` 环境变量并重启 AstrBot，即可恢复真实账本。

## 已知限制

- 仅提供支出记账，暂不支持收入、指定日期、分类管理等指令。
- `/导入` 只接受平台提供的**文件消息段**；把链接、图片或普通文本发过来不会被当作账本文件。
- `/导出` 发送文件依赖平台适配器支持 `File` 消息段；不支持时只发送摘要和错误提示。
- 单次导入文件最大 20 MB；导入、导出文件保留 24 小时，每 6 小时清理一次。
- `/导入` 等待期间，该会话的后续消息会优先交给等待逻辑接管；非文件消息不会触发正常回复，也不会执行导入。
- 白名单按完整 UMO 精确匹配，不支持通配符；修改配置后建议重载插件。
- 每个插件实例只维护一个 MCP 子进程，所有指令串行执行。
- MCP 启动最长等待 20 秒，单次工具调用最长等待 30 秒；超时会关闭连接，下次调用重新启动。
- 需要账本软件及 `mucage_mcp.py` 已存在；插件不负责安装账本软件。
- `mcp_command` 必须能在 AstrBot 的运行环境中执行；Docker 部署时路径必须是容器内路径。
- 插件只提供固定指令，不注册 LLM 工具，不会让模型自行记账。
- 群聊白名单授权的是整个会话，无法区分会话内具体成员。
- 账本余额与合并逻辑由账本软件的 MCP 服务负责，插件只展示其返回值。
- 攒着/可用只是账面划分，不会真正转账；自动攒着由账本服务按周期执行，插件只负责配置。
- 自动攒着选择「计入」时，账本服务会生成分类为「攒着」的支出记录，并计入限额与统计。
- 余额拆分与自动攒着需要账本软件 10.27.3 或更高版本；旧版会缺少相关字段或不支持相关工具。
- 插件没有提供设置总余额的指令；如需调整总余额，请使用桌面端或直接调用 MCP 的 `set_balance`。
- 导入、导出文件包含完整账本数据，请勿将 `plugin_data` 目录或导出文件随意分享。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `未配置 mucage_mcp.py 路径` | 在插件配置中填写 `mcp_script_path` |
| `找不到 mucage_mcp.py` | 检查路径是否正确、文件是否存在 |
| `No module named mcp` | 在 AstrBot 运行环境中安装 `requirements.txt` |
| `记账服务不可用：...` | 检查 `mcp_command` 和脚本路径；可在终端手动运行 `python mucage_mcp.py` 验证 |
| 工具返回 `金额必须大于 0` | 这是账本工具的原样错误提示，修正金额后重试 |
| `/攒着`、`/可用`、`/自动攒着` 报未知工具 | 升级账本软件到 10.27.3 或更高版本 |
| `/余额` 没有攒着/可用行 | 账本服务版本过旧，升级到 10.27.3 或更高版本 |
| `导入失败：文件不是有效的账本同步文件。` | 确认发送的是账本软件导出的 `mucage-ledger` JSON |
| `导入失败：未能接收到文件。` | 在 2 分钟内发送账本文件；确认平台把文件作为文件消息段传递 |
| 导出后没有收到文件 | 当前平台可能不支持 `File` 消息段，查看文字错误提示 |
| 指令无响应且提示未授权 | 将当前会话 UMO 加入 `allowed_sessions` |

## 卸载

1. 在 WebUI 停用插件（会触发 `terminate`，关闭 MCP 子进程并停止清理任务）。
2. 删除插件目录。
3. 如需彻底清理，删除：
   - 配置 `data/config/astrbot_plugin_SanBaiLIang_config.json`
   - 导入导出文件 `data/plugin_data/astrbot_plugin_SanBaiLIang/`

## 许可证

本项目以 **GNU Affero General Public License v3.0 only（AGPL-3.0-only）** 开源，完整条款见
[LICENSE](LICENSE)。

Copyright (C) 2026 Mucage。

如果修改本项目并通过网络提供服务，请按 AGPL-3.0 的要求向用户提供对应源码。

## 参考

- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot MCP 使用文档](https://docs.astrbot.app/use/mcp.html)
- 插件按官方 `Star` / `register` / `filter`、`session_waiter` 与 `_conf_schema.json` 配置机制实现。
