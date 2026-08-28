# -*- coding: utf-8 -*-
"""astrbot_plugin_server_ops — 服务器管理工具包插件。

为 AstrBot 提供「电脑/服务器控制」能力，全部封装为 LLM 工具（llm_tool），
机器人可以在对话中直接调用；同时提供少量手动指令。

工具清单（供 LLM 调用）：
- server_status         系统状态总览（CPU / 内存 / 负载 / 运行时间 / 磁盘）
- server_disk_usage     磁盘使用详情
- server_process_list   进程列表（按 CPU / 内存排序）
- server_service_ops    systemd 服务查询与管理（启停需管理员）
- server_log_view       查看日志文件末尾 N 行（仅允许的路径）
- server_exec           执行自定义 shell 命令（仅管理员，带安全黑名单）
- plugin_index          已安装插件能力索引（名称 / 描述 / 命令 / 工具）
- astrbot_status        AstrBot 自身状态（版本 / 进程 / 插件数）
- gsuid_status          gsuid_core 运行状态

安全设计：
- 默认 require_admin=True：非管理员调用会被拒绝（可配置 allowed_user_ids 白名单）。
- server_exec / 服务启停等写操作强制要求管理员。
- server_exec 内置危险命令黑名单，禁止 rm -rf / sudo / shutdown / 重启等。
- 所有命令执行带超时，防止卡死。
"""

import os
import re
import subprocess
import time
from pathlib import Path

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# --------------------------------------------------------------------------- #
# 危险命令黑名单（与 AstrBot 内置 local computer-use 保持一致并加强）          #
# --------------------------------------------------------------------------- #
_BLOCKED_PATTERNS = [
    " rm -rf ",
    " rm -fr ",
    " rm -r ",
    " mkfs",
    " dd if=",
    " shutdown",
    " reboot",
    " poweroff",
    " halt",
    " sudo ",
    ":(){:|:&};:",
    " kill -9 ",
    " killall",
    " pkill",
    " chmod -r",
    " chown ",
    " > /dev/sda",
    " mkfs.",
    " fdisk",
    " parted ",
    " > /etc/passwd",
    " : > ",
    " git push --force",
    " rm -R",
]

_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:+-]+$")


def _is_safe_command(command: str) -> bool:
    """检查命令是否安全（不含危险子串）。"""
    if not command or not command.strip():
        return False
    cmd = f" {command.strip().lower()} "
    return not any(pat in cmd for pat in _BLOCKED_PATTERNS)


class Main(star.Star):
    def __init__(self, context: star.Context, config: dict = None):
        super().__init__(context, config)
        self.config = config or {}

    # ------------------------------------------------------------------ #
    # 权限                                                               #
    # ------------------------------------------------------------------ #

    def _has_perm(self, event: AstrMessageEvent) -> bool:
        """是否有权调用工具：管理员 或 在 allowed_user_ids 白名单内。"""
        if not self.config.get("require_admin", True):
            return True
        if event.is_admin():
            return True
        allowed = [str(x) for x in (self.config.get("allowed_user_ids") or [])]
        return str(event.get_sender_id()) in allowed

    def _denied(self, op: str) -> str:
        return (
            f"权限不足：{op} 仅限管理员（或在插件配置 allowed_user_ids 中白名单的用户）使用。\n"
            f"你的用户 ID 是 {self._sender_id()}，可在 AstrBot 面板「配置-通用配置」的管理员列表中加入，"
            f"或在插件设置里把它加进 allowed_user_ids。"
        )

    def _sender_id(self, event: AstrMessageEvent = None) -> str:
        try:
            if event is not None:
                return str(event.get_sender_id())
        except Exception:
            pass
        return "未知"

    # ------------------------------------------------------------------ #
    # 命令执行助手                                                       #
    # ------------------------------------------------------------------ #

    def _run_cmd(
        self,
        cmd: str,
        timeout: int = None,
        cwd: str = None,
    ) -> dict:
        """执行 shell 命令，返回 {ok, stdout, stderr, code}。输出截断防超长。"""
        timeout = timeout or int(self.config.get("command_timeout", 20) or 20)
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env={**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"},
            )
            return {
                "ok": True,
                "stdout": (result.stdout or "")[-4000:],
                "stderr": (result.stderr or "")[-2000:],
                "code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"命令执行超时（>{timeout}s），已终止。",
                "code": -1,
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "stdout": "", "stderr": str(e), "code": -2}

    def _fmt(self, r: dict, extra: str = "") -> str:
        """格式化命令结果。"""
        out = (r.get("stdout") or "").strip()
        err = (r.get("stderr") or "").strip()
        lines = []
        if out:
            lines.append(out)
        if err:
            lines.append(f"[stderr] {err}")
        if not lines:
            lines.append("(无输出)")
        if extra:
            lines.insert(0, extra)
        lines.append(f"[exit code: {r.get('code')}]")
        return "\n".join(lines)

    def _read_text(self, p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # 工具：系统状态                                                     #
    # ------------------------------------------------------------------ #

    @llm_tool(name="server_status")
    async def server_status(self, event: AstrMessageEvent, *args, **kwargs) -> str:
        """查看服务器系统状态总览：CPU 使用率、内存、负载、运行时间、磁盘占用。"""
        if not self._has_perm(event):
            return self._denied("系统状态查看")

        # CPU 使用率（读取两次 /proc/stat 间隔约 0.2s）
        try:
            def _cpu():
                with open("/proc/stat", encoding="utf-8") as f:
                    parts = f.readline().split()
                total = sum(int(x) for x in parts[1:])
                idle = int(parts[4])
                return total, idle

            t1, i1 = _cpu()
            time.sleep(0.2)
            t2, i2 = _cpu()
            total_delta = max(t2 - t1, 1)
            idle_delta = i2 - i1
            cpu_pct = round(100 * (total_delta - idle_delta) / total_delta, 1)
        except Exception:
            cpu_pct = None

        # 内存
        mem_line = ""
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                info = {}
                for line in f:
                    k, _, v = line.partition(":")
                    info[k.strip()] = int(v.strip().split()[0])
            mem_total = info.get("MemTotal", 0) / 1024
            mem_avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024
            mem_used = mem_total - mem_avail
            mem_line = f"内存: 已用 {mem_used:.0f} MB / 总计 {mem_total:.0f} MB（{100 * mem_used / max(mem_total, 1):.0f}%）"
        except Exception:
            mem_line = "内存: 读取失败"

        # 负载 + 运行时间
        try:
            load = open("/proc/loadavg", encoding="utf-8").read().strip().split()
            load_str = " / ".join(load[:3])
        except Exception:
            load_str = "?"
        try:
            uptime_s = float(open("/proc/uptime", encoding="utf-8").read().split()[0])
            up_days = int(uptime_s // 86400)
            up_hms = time.strftime("%H:%M:%S", time.gmtime(uptime_s % 86400))
            uptime_str = f"{up_days}天 {up_hms}"
        except Exception:
            uptime_str = "?"

        # 磁盘
        disk = self._run_cmd("df -h / /root /home 2>/dev/null | head -20", timeout=10)

        hostname = self._run_cmd("hostname 2>/dev/null", timeout=5)["stdout"].strip()
        cpu_line = f"CPU: {cpu_pct}%" if cpu_pct is not None else "CPU: 读取失败"

        return (
            f"【服务器状态】{hostname}\n"
            f"{cpu_line}\n"
            f"{mem_line}\n"
            f"负载(1/5/15分钟): {load_str}\n"
            f"运行时间: {uptime_str}\n\n"
            f"磁盘:\n{disk['stdout'].strip() or '(无磁盘信息)'}"
        )

    @llm_tool(name="server_disk_usage")
    async def server_disk_usage(self, event: AstrMessageEvent, *args, **kwargs) -> str:
        """查看服务器磁盘使用详情（各挂载点 / 目录占用）。"""
        if not self._has_perm(event):
            return self._denied("磁盘查看")

        df = self._run_cmd("df -h 2>/dev/null | head -30", timeout=10)
        return self._fmt(df, "【磁盘使用】")

    @llm_tool(name="server_process_list")
    async def server_process_list(
        self,
        event: AstrMessageEvent,
        sort_by: str = "cpu",
        count: int = 15,
    ) -> str:
        """查看服务器当前进程列表，按 CPU 或内存占用排序。

        Args:
            sort_by(string): 排序方式，cpu 或 mem，默认 cpu。
            count(number): 显示前 N 个进程，默认 15，最大 50。
        """
        if not self._has_perm(event):
            return self._denied("进程查看")

        count = max(1, min(int(count or 15), 50))
        key = "pcpu" if str(sort_by).lower() in ("cpu", "pcpu") else "pmem"
        cmd = f"ps -eo pid,user,pcpu,pmem,rss,comm --sort=-{key} 2>/dev/null | head -{count + 1}"
        r = self._run_cmd(cmd, timeout=10)
        return self._fmt(r, f"【进程 TOP {count}（按 {key} 排序）】")

    # ------------------------------------------------------------------ #
    # 工具：服务管理                                                     #
    # ------------------------------------------------------------------ #

    @llm_tool(name="server_service_ops")
    async def server_service_ops(
        self,
        event: AstrMessageEvent,
        action: str,
        service: str = "",
    ) -> str:
        """管理系统服务（systemd）。status/list 需管理员或白名单；start/stop/restart/enable/disable 仅限管理员。

        Args:
            action(string): 操作：status（查单个服务）、list（列出服务）、start、stop、restart、enable、disable。
            service(string): 服务名，例如 nginx、docker、astrbot。action=list 时可为空。
        """
        action = str(action or "").strip().lower()
        if action not in ("status", "list", "start", "stop", "restart", "enable", "disable"):
            return "无效操作。可用: status / list / start / stop / restart / enable / disable"

        if action == "list":
            if not self._has_perm(event):
                return self._denied("服务列表查看")
            r = self._run_cmd("systemctl list-units --type=service --no-pager 2>/dev/null | head -40", timeout=15)
            return self._fmt(r, "【系统服务列表】")

        service = str(service or "").strip()
        if not service:
            return "请提供服务名，例如：server_service_ops(action='status', service='nginx')"
        if not _SERVICE_NAME_RE.match(service):
            return f"服务名不合法：{service}"

        if action in ("start", "stop", "restart", "enable", "disable"):
            if not event.is_admin():
                return self._denied(f"服务 {action}")
        else:  # status
            if not self._has_perm(event):
                return self._denied("服务状态查看")

        cmd = f"systemctl {action} {service} --no-pager 2>&1 | head -30"
        r = self._run_cmd(cmd, timeout=30)
        return self._fmt(r, f"【systemctl {action} {service}】")

    # ------------------------------------------------------------------ #
    # 工具：日志                                                         #
    # ------------------------------------------------------------------ #

    @llm_tool(name="server_log_view")
    async def server_log_view(
        self,
        event: AstrMessageEvent,
        path: str,
        lines: int = 50,
    ) -> str:
        """查看日志或文本文件的末尾 N 行（路径必须位于允许的根目录内，防止越权读取）。

        Args:
            path(string): 日志文件绝对路径，例如 /var/log/nginx/error.log。
            lines(number): 读取末尾行数，默认 50，最大 200。
        """
        if not self._has_perm(event):
            return self._denied("日志查看")

        lines = max(1, min(int(lines or 50), 200))
        raw = str(path or "").strip()
        if not raw:
            return "请提供日志文件路径。"

        try:
            p = Path(raw).expanduser().resolve()
        except Exception:
            return f"路径解析失败：{raw}"

        # 允许的根目录
        astrbot_data = str(Path(get_astrbot_data_path()).resolve())
        roots = [astrbot_data, "/var/log", "/tmp", "/root"]
        for extra in (self.config.get("log_roots") or []):
            try:
                roots.append(str(Path(str(extra)).expanduser().resolve()))
            except Exception:
                pass

        if not any(str(p).startswith(root.rstrip("/") + "/") or str(p) == root for root in roots):
            return (
                f"路径不在允许范围内：{p}\n"
                f"允许的根目录：{', '.join(roots)}。\n"
                "可在插件配置 log_roots 中增加允许的目录。"
            )

        if not p.exists() or not p.is_file():
            return f"文件不存在或不是普通文件：{p}"

        r = self._run_cmd(f"tail -n {lines} '{p}' 2>&1", timeout=10)
        return self._fmt(r, f"【{p} 末尾 {lines} 行】")

    # ------------------------------------------------------------------ #
    # 工具：自定义命令（仅管理员）                                       #
    # ------------------------------------------------------------------ #

    @llm_tool(name="server_exec")
    async def server_exec(self, event: AstrMessageEvent, command: str) -> str:
        """在服务器上执行一条 shell 命令并返回输出。仅限管理员使用；危险命令（rm -rf、sudo、关机重启、格式化磁盘、kill -9 等）会被拦截。

        Args:
            command(string): 要执行的 shell 命令，例如：ls -la /root；df -h；free -m。
        """
        if not event.is_admin():
            return self._denied("shell 命令执行")

        cmd = str(command or "").strip()
        if not cmd:
            return "请提供要执行的命令。"
        if not _is_safe_command(cmd):
            return (
                "该命令包含危险操作，已被拦截。禁止的命令包括：rm -rf / rm -fr、sudo、"
                "shutdown/reboot/poweroff/halt、mkfs/fdisk/parted、dd if=、kill -9/pkill/killall 等。\n"
                "如需这些操作，请直接在服务器上手动执行。"
            )

        r = self._run_cmd(cmd, timeout=int(self.config.get("command_timeout", 20) or 20))
        return self._fmt(r, f"【执行: {cmd}】")

    # ------------------------------------------------------------------ #
    # 工具：插件能力索引                                                 #
    # ------------------------------------------------------------------ #

    @llm_tool(name="plugin_index")
    async def plugin_index(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """查看 AstrBot 已安装的插件能力索引：每个插件的名称、描述、可用命令与 LLM 工具。用于了解机器人自身具备哪些插件能力并正确调用。

        Args:
            keyword(string): 可选，按关键词过滤插件（匹配名称/描述）。
        """
        if not self._has_perm(event):
            return self._denied("插件索引查看")

        plugins_root = Path(get_astrbot_data_path()) / "plugins"
        if not plugins_root.exists():
            return "未找到插件目录。"

        # 读取被停用的插件，标注哪些真正可用
        disabled = set()
        try:
            from astrbot.api import sp

            disabled = {
                str(x).split(".")[-2] for x in sp.get(
                    "inactivated_plugins", [], scope="global", scope_id="global"
                )
            }
        except Exception:
            pass

        results = []
        for p in sorted(plugins_root.iterdir()):
            if not p.is_dir():
                continue
            meta = p / "metadata.yaml"
            if not meta.exists():
                continue
            info = self._parse_metadata(meta)
            name = info.get("name") or p.name
            display = info.get("display_name") or name
            desc = info.get("desc") or ""
            version = info.get("version") or ""
            if keyword and not (
                keyword.lower() in display.lower()
                or keyword.lower() in desc.lower()
                or keyword.lower() in name.lower()
            ):
                continue

            commands, tools = self._scan_plugin_api(p)
            cmds = "、".join(commands[:12]) if commands else "（无）"
            tls = "、".join(tools[:12]) if tools else "（无）"
            disabled_flag = " ⛔已停用" if name in disabled else ""
            results.append(
                f"■ {display}（{name}）{version}{disabled_flag}\n"
                f"  描述: {desc[:120]}\n"
                f"  命令: {cmds}\n"
                f"  LLM工具: {tls}"
            )
            if len(results) >= 40:
                results.append("...（插件较多，已截断）")
                break

        if not results:
            return "未找到匹配的插件。" if keyword else "未找到已安装插件。"
        return "【已安装插件能力索引】（⛔ 表示插件已被停用，工具不可调用）\n" + "\n\n".join(results)

    def _parse_metadata(self, path: Path) -> dict:
        """简易解析 metadata.yaml（无需 yaml 依赖，仅取键值行）。"""
        info = {}
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.rstrip()
                if not line or line.startswith(("#", "-", "  ")):
                    continue
                if ":" in line:
                    k, _, v = line.partition(":")
                    info[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass
        return info

    def _scan_plugin_api(self, plugin_dir: Path) -> tuple:
        """扫描插件 main.py，提取命令与 LLM 工具名（正则，不导入代码）。"""
        commands, tools = [], []
        for mf in plugin_dir.glob("*.py"):
            try:
                src = mf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for m in re.finditer(r"@filter\.command(?:_group)?\(\s*[\"']([^\"']+)[\"']", src):
                if m.group(1) not in commands:
                    commands.append(m.group(1))
            for m in re.finditer(r"@(?:filter\.)?llm_tool\(\s*name\s*=\s*[\"']([^\"']+)[\"']", src):
                if m.group(1) not in tools:
                    tools.append(m.group(1))
        return commands, tools

    # ------------------------------------------------------------------ #
    # 工具：AstrBot / gsuid_core 状态                                    #
    # ------------------------------------------------------------------ #

    @llm_tool(name="astrbot_status")
    async def astrbot_status(self, event: AstrMessageEvent, *args, **kwargs) -> str:
        """查看 AstrBot 自身运行状态：版本、进程占用、数据目录大小、已安装插件数量。"""
        if not self._has_perm(event):
            return self._denied("AstrBot 状态查看")

        try:
            from importlib.metadata import version

            ver = version("astrbot")
        except Exception:
            ver = "?"

        pid_info = self._run_cmd(
            "ps -eo pid,rss,etime,cmd | grep -i 'astrbot.cli' | grep -v grep | head -3",
            timeout=10,
        )
        plugins = self._run_cmd(
            f"ls -d {Path(get_astrbot_data_path()) / 'plugins'}/*/ 2>/dev/null | wc -l",
            timeout=10,
        )
        data_size = self._run_cmd(
            f"du -sh {Path(get_astrbot_data_path())} 2>/dev/null | cut -f1",
            timeout=30,
        )

        return (
            f"【AstrBot 状态】\n"
            f"版本: {ver}\n"
            f"数据目录: {get_astrbot_data_path()}\n"
            f"数据占用: {data_size['stdout'].strip() or '?'}\n"
            f"已安装插件: {plugins['stdout'].strip() or '?'} 个\n"
            f"进程:\n{pid_info['stdout'].strip() or '(未找到进程)'}"
        )

    @llm_tool(name="gsuid_status")
    async def gsuid_status(self, event: AstrMessageEvent, *args, **kwargs) -> str:
        """查看 gsuid_core（原神小助手）运行状态：进程与监听端口。"""
        if not self._has_perm(event):
            return self._denied("gsuid_core 状态查看")

        keyword = str(self.config.get("gsuid_process", "gsuid_core") or "gsuid_core")
        proc = self._run_cmd(
            f"ps -eo pid,etime,rss,cmd | grep '{keyword}' | grep -v grep | head -5",
            timeout=10,
        )
        port = self._run_cmd("ss -tlnp 2>/dev/null | grep -iE 'python|gsuid' | head -10", timeout=10)

        return (
            f"【gsuid_core 状态】（匹配关键字: {keyword}）\n"
            f"进程:\n{proc['stdout'].strip() or '(未找到 gsuid_core 进程)'}\n\n"
            f"监听端口:\n{port['stdout'].strip() or '(无)'}"
        )

    # ------------------------------------------------------------------ #
    # 指令                                                               #
    # ------------------------------------------------------------------ #

    @filter.command("服务器")
    async def cmd_server_help(self, event: AstrMessageEvent):
        """查看服务器管理工具包的用法与工具清单"""
        perm = "管理员/白名单" if self.config.get("require_admin", True) else "所有人"
        yield event.plain_result(
            "🖥️ 服务器管理工具包（astrbot_plugin_server_ops）\n"
            "机器人可通过以下 LLM 工具控制/查看本机：\n"
            "· server_status — 系统状态总览\n"
            "· server_disk_usage — 磁盘使用\n"
            "· server_process_list — 进程列表\n"
            "· server_service_ops — systemd 服务管理（启停需管理员）\n"
            "· server_log_view — 查看日志（限允许路径）\n"
            "· server_exec — 执行 shell 命令（仅管理员，带安全拦截）\n"
            "· plugin_index — 已安装插件能力索引\n"
            "· astrbot_status / gsuid_status — 状态查看\n\n"
            f"当前权限要求: {perm}\n"
            "手动指令: /服务器状态"
        )

    @filter.command("服务器状态")
    async def cmd_server_status(self, event: AstrMessageEvent):
        """查看系统状态速览"""
        if not self._has_perm(event):
            yield event.plain_result(self._denied("系统状态查看"))
            return
        yield event.plain_result(await self.server_status(event))
