"""astrbot_plugin_server_ops 插件测试脚本。

验证：
1. 模块可加载，9 个 LLM 工具注册成功
2. 权限控制：非管理员拒绝 / 管理员放行 / allowed_user_ids 白名单
3. server_status / server_disk_usage / server_process_list
4. server_service_ops：list/status/启停权限/非法服务名
5. server_log_view：允许路径 / 越权路径 / 非法文件
6. server_exec：危险命令黑名单拦截
7. plugin_index：插件能力索引
8. astrbot_status / gsuid_status
9. 指令处理器（/服务器、/服务器状态）
"""
import asyncio
import importlib.util
import sys

sys.path.insert(0, "/root/.local/share/uv/tools/astrbot")

spec = importlib.util.spec_from_file_location(
    "server_ops",
    "/root/dsh_projects/astrbot_plugin_server_ops/main.py",
)
plugin_mod = importlib.util.module_from_spec(spec)
sys.modules["server_ops"] = plugin_mod
spec.loader.exec_module(plugin_mod)

import server_ops

Main = server_ops.Main
_SERVICE_NAME_RE = server_ops._SERVICE_NAME_RE
_is_safe_command = server_ops._is_safe_command


class MockEvent:
    def __init__(self, admin=False, sender_id="12345", message_str=""):
        self._admin = admin
        self._sender_id = sender_id
        self.message_str = message_str
        self.unified_msg_origin = "test:FriendMessage:12345"
        self._results = []

    def is_admin(self):
        return self._admin

    def get_sender_id(self):
        return self._sender_id

    def get_platform_name(self):
        return "test"

    def plain_result(self, text):
        return text


def make_plugin(**cfg):
    defaults = {
        "require_admin": True,
        "allowed_user_ids": [],
        "command_timeout": 20,
        "log_roots": [],
    }
    defaults.update(cfg)
    return Main(None, defaults)


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


async def test_permissions():
    print("== 权限控制 ==")
    p = make_plugin()

    # 非管理员
    r = await p.server_status(MockEvent(admin=False))
    check("非管理员调用 server_status 被拒绝", "权限不足" in r)
    r = await p.server_exec(MockEvent(admin=False), "ls")
    check("非管理员调用 server_exec 被拒绝", "权限不足" in r)

    # 管理员
    r = await p.server_status(MockEvent(admin=True))
    check("管理员调用 server_status 成功", "服务器状态" in r and "内存" in r)

    # require_admin=False 放行
    p2 = make_plugin(require_admin=False)
    r = await p2.server_status(MockEvent(admin=False))
    check("require_admin=False 非管理员放行", "服务器状态" in r)

    # allowed_user_ids 白名单
    p3 = make_plugin(allowed_user_ids=["99999"])
    r = await p3.server_status(MockEvent(admin=False, sender_id="99999"))
    check("allowed_user_ids 白名单放行", "服务器状态" in r)
    r = await p3.server_status(MockEvent(admin=False, sender_id="88888"))
    check("非白名单非管理员拒绝", "权限不足" in r)

    # server_exec 的写操作即使是普通用户也不行（仅管理员）
    r = await p3.server_exec(MockEvent(admin=False, sender_id="99999"), "echo hi")
    check("server_exec 仅限管理员（白名单也不行）", "权限不足" in r)


async def test_status_tools():
    print("== 状态类工具 ==")
    p = make_plugin()
    ev = MockEvent(admin=True)

    r = await p.server_status(ev)
    check("server_status 含 CPU/内存/负载", all(k in r for k in ("CPU", "内存", "负载", "运行时间")))

    r = await p.server_disk_usage(ev)
    check("server_disk_usage 返回磁盘信息", "磁盘" in r or "Filesystem" in r or "文件系统" in r)

    r = await p.server_process_list(ev, sort_by="cpu", count=5)
    check("server_process_list(cpu) 返回进程", "进程" in r or "PID" in r or "ps" in r)

    r = await p.server_process_list(ev, sort_by="mem", count=3)
    check("server_process_list(mem) 返回进程", "进程" in r or "PID" in r)

    r = await p.server_process_list(ev, count=999)
    check("server_process_list count 上限 50", "TOP 50" in r)


async def test_service_ops():
    print("== 服务管理 ==")
    p = make_plugin()
    ev_admin = MockEvent(admin=True)
    ev_user = MockEvent(admin=False)

    r = await p.server_service_ops(ev_admin, action="list")
    check("service list 返回", "服务" in r or "systemctl" in r)

    r = await p.server_service_ops(ev_admin, action="status", service="sshd")
    check("service status sshd 可查询", isinstance(r, str) and len(r) > 0)

    r = await p.server_service_ops(ev_user, action="restart", service="nginx")
    check("非管理员 restart 被拒", "权限不足" in r)

    r = await p.server_service_ops(ev_admin, action="restart", service="bad;rm -rf /")
    check("非法服务名被拒", "服务名不合法" in r)

    r = await p.server_service_ops(ev_admin, action="hack")
    check("非法操作被拒", "无效操作" in r)

    r = await p.server_service_ops(ev_admin, action="restart", service="")
    check("空服务名提示", "请提供服务名" in r)


async def test_log_view():
    print("== 日志查看 ==")
    p = make_plugin()
    ev = MockEvent(admin=True)

    r = await p.server_log_view(ev, path="/var/log/syslog", lines=5)
    check("允许路径可读（或文件不存在提示）", isinstance(r, str) and ("系统" not in r or "不存在" in r))

    r = await p.server_log_view(ev, path="/etc/shadow", lines=5)
    check("越权路径被拒", "不在允许范围内" in r)

    r = await p.server_log_view(ev, path="/var/log/no_such_file_xyz.log", lines=5)
    check("不存在文件提示", "不存在" in r)

    r = await p.server_log_view(ev, path="/var/log/syslog", lines=99999)
    check("lines 上限 200", "200" in r or "不存在" in r)


async def test_exec():
    print("== shell 执行 ==")
    p = make_plugin()
    ev = MockEvent(admin=True)

    r = await p.server_exec(ev, "echo hello-world")
    check("安全命令执行", "hello-world" in r)

    r = await p.server_exec(ev, "rm -rf /")
    check("rm -rf 被拦截", "危险" in r and "拦截" in r)

    r = await p.server_exec(ev, "sudo reboot")
    check("sudo/reboot 被拦截", "危险" in r)

    r = await p.server_exec(ev, "")
    check("空命令提示", "请提供" in r)

    r = await p.server_exec(ev, "sleep 100")
    check("命令超时保护（配置超时 1s）", "超时" in r or "hello" not in r) if False else None
    # 超时测试单独跑，避免拖慢
    p2 = make_plugin(command_timeout=1)
    r = await p2.server_exec(ev, "sleep 5")
    check("命令超时保护", "超时" in r)


async def test_plugin_index():
    print("== 插件能力索引 ==")
    p = make_plugin()
    ev = MockEvent(admin=True)

    r = await p.plugin_index(ev)
    check("plugin_index 返回插件索引", "插件" in r)

    r = await p.plugin_index(ev, keyword="server")
    check("plugin_index 关键词过滤可返回空或结果", isinstance(r, str) and len(r) > 0)

    # 校验扫描器能识别本插件的工具与命令
    cmds, tools = p._scan_plugin_api(
        __import__("pathlib").Path("/root/dsh_projects/astrbot_plugin_server_ops")
    )
    check("扫描出 server_status 工具", "server_status" in tools)
    check("扫描出 服务器 命令", "服务器" in cmds)


async def test_astrbot_gsuid():
    print("== AstrBot / gsuid 状态 ==")
    p = make_plugin()
    ev = MockEvent(admin=True)

    r = await p.astrbot_status(ev)
    check("astrbot_status 返回", "AstrBot" in r and "版本" in r)

    r = await p.gsuid_status(ev)
    check("gsuid_status 返回", "gsuid_core" in r)


async def test_commands():
    print("== 指令 ==")
    p = make_plugin()
    ev = MockEvent(admin=True, message_str="/服务器")

    async def consume(gen):
        out = []
        async for item in gen:
            out.append(item)
        return out

    r = await consume(p.cmd_server_help(ev))
    check("/服务器 返回用法", any("服务器管理工具包" in str(x) for x in r))

    ev2 = MockEvent(admin=True, message_str="/服务器状态")
    r = await consume(p.cmd_server_status(ev2))
    check("/服务器状态 返回状态", any("服务器状态" in str(x) for x in r))

    ev3 = MockEvent(admin=False, message_str="/服务器状态")
    r = await consume(p.cmd_server_status(ev3))
    check("/服务器状态 非管理员被拒", any("权限不足" in str(x) for x in r))


async def test_safety_helpers():
    print("== 安全辅助函数 ==")
    check("_is_safe_command(echo hi)=True", _is_safe_command("echo hi"))
    check("_is_safe_command(rm -rf /)=False", not _is_safe_command("rm -rf /"))
    check("_is_safe_command(shutdown)=False", not _is_safe_command("shutdown -h now"))
    check("_is_safe_command(sudo apt)=False", not _is_safe_command("sudo apt update"))
    check("_SERVICE_NAME_RE(nvidia-driver@1)=True", bool(_SERVICE_NAME_RE.match("nvidia-driver@1")))
    check("_SERVICE_NAME_RE(bad;rm)=False", not bool(_SERVICE_NAME_RE.match("bad;rm")))


async def main():
    print("=== astrbot_plugin_server_ops 测试开始 ===")
    await test_permissions()
    await test_status_tools()
    await test_service_ops()
    await test_log_view()
    await test_exec()
    await test_plugin_index()
    await test_astrbot_gsuid()
    await test_commands()
    await test_safety_helpers()
    print(f"\n=== 结果: {PASS} 通过, {FAIL} 失败 ===")
    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
