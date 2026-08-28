# astrbot_plugin_server_ops — 服务器管理工具包

> **作者：yvdi-abc（yudi）** · 版本 v1.0.0 · [GitHub](https://github.com/yvdi-abc/astrbot_plugin_server_ops)

为 [AstrBot](https://github.com/soulter/AstrBot) 提供**安全的电脑/服务器控制能力**。所有功能封装为 **LLM 工具（llm_tool）**，机器人可以在对话中直接调用，也提供少量手动指令。

> 本插件与 AstrBot 内置的 Computer Use（`astrbot_execute_shell` 等）互补：
> 内置工具偏「通用 shell/python/文件」，本插件提供「服务器运维场景」的中文封装：系统状态、服务管理、日志、插件能力索引、AstrBot/gsuid_core 状态。

## 功能总览

| LLM 工具 | 说明 | 权限 |
|---|---|---|
| `server_status` | 系统状态总览：CPU / 内存 / 负载 / 运行时间 / 磁盘 | 管理员或白名单 |
| `server_disk_usage` | 磁盘使用详情 | 管理员或白名单 |
| `server_process_list` | 进程列表（按 CPU/内存排序） | 管理员或白名单 |
| `server_service_ops` | systemd 服务：list / status / start / stop / restart / enable / disable | 启停类仅管理员 |
| `server_log_view` | 查看日志文件末尾 N 行（仅允许的路径） | 管理员或白名单 |
| `server_exec` | 执行自定义 shell 命令（带危险命令黑名单） | 仅管理员 |
| `plugin_index` | 已安装插件能力索引（名称/描述/命令/工具/停用标注） | 管理员或白名单 |
| `astrbot_status` | AstrBot 版本 / 进程 / 数据占用 / 插件数 | 管理员或白名单 |
| `gsuid_status` | gsuid_core 进程与端口状态 | 管理员或白名单 |

手动指令：

| 指令 | 说明 |
|---|---|
| `/服务器` | 查看插件用法与工具清单 |
| `/服务器状态` | 系统状态速览 |

## 安装

1. 将 `astrbot_plugin_server_ops` 整个文件夹放入 AstrBot 的插件目录：
   ```
   <AstrBot数据目录>/plugins/astrbot_plugin_server_ops
   ```
2. 在 AstrBot 管理面板「插件管理」中启用本插件（或在 `plugin_set` 中加入 `astrbot_plugin_server_ops`）。
3. 重启 AstrBot（或重载插件）。

## 配置（`_conf_schema.json`）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `require_admin` | `true` | 是否仅管理员/白名单可用。建议保持开启 |
| `allowed_user_ids` | `[]` | 额外允许的用户 ID 列表（QQ 号等，用 `/sid` 查自己的 ID） |
| `command_timeout` | `20` | 命令执行超时（秒） |
| `log_roots` | `["/var/log", "/tmp"]` | `server_log_view` 允许查看的日志根目录（AstrBot 数据目录自动包含） |
| `gsuid_process` | `gsuid_core` | `gsuid_status` 的进程匹配关键字 |

## 权限说明

- **管理员判定**：AstrBot 全局配置 `admins_id` 中的用户 ID，或在插件 `allowed_user_ids` 白名单中的用户。
- **写操作**（`server_exec`、服务 start/stop/restart 等）**强制要求管理员**，白名单不算。
- 非管理员调用会收到明确的权限拒绝提示（含自己的用户 ID 与设置指引）。

## 安全设计

- `server_exec` 内置危险命令黑名单，拦截：`rm -rf` / `rm -fr`、`sudo`、`shutdown` / `reboot` / `poweroff` / `halt`、`mkfs` / `fdisk` / `parted`、`dd if=`、`kill -9` / `pkill` / `killall`、`chown` 等。
- 服务名严格校验（仅字母数字 `_ . @ : + -`），杜绝命令注入。
- `server_log_view` 只允许读取配置的根目录内的文件，防止越权读取敏感文件。
- 所有命令带超时（默认 20s），防止卡死机器人。

## 测试

```bash
# 使用 AstrBot 的 Python 环境运行
<AstrBot目录>/bin/python test_plugin.py
```

测试覆盖：权限控制、状态/磁盘/进程/服务/日志/执行/插件索引/AstrBot/gsuid 工具、指令处理器、安全辅助函数。

## 让机器人「更好地使用」配套建议

1. **开启 AstrBot 内置 Computer Use**：配置 `provider_settings.computer_use_runtime = "local"`（本插件不依赖它，但可叠加使用内置的 `astrbot_execute_shell` / 文件读写工具）。
2. **省 token**：`provider_settings.tool_schema_mode = "skills_like"`（首轮只发工具名+描述，调用时才回传完整参数，显著降低 tool schema 开销；代价是调用工具时多一次模型回查）。
3. **让机器人知道装了哪些插件**：对话里说「看看你有哪些插件/能力」，机器人会调用 `plugin_index` 输出能力索引。
4. 被停用的插件（如 enhance_mode）的 LLM 工具无法调用；`plugin_index` 会标注 ⛔。如需启用，在面板「插件管理」中启用对应插件。

## 变更记录

- v1.0.0：首个版本。9 个 LLM 工具 + 2 个指令，权限与安全设计齐全。
