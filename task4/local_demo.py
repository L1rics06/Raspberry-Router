"""真实上游网关及Agent搭配模拟设备和本机规则模型的闭环验证"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .common import ROOT, check, inspect_gateway_result, new_report, write_json
from .deploy import make_agent_config
from .gateway_checks import chat_once, login, manual, run_gateway
from .profile import load_profile
from .layout import server_repo, agent_repo as resolve_agent_repo


class RuleHandler(BaseHTTPRequestHandler):
    """仅监听回环地址的OpenAI兼容规则夹具，不调用付费模型"""

    # log_message 表示屏蔽HTTP访问日志，参数由标准库传入
    # 防止请求信息或凭证进入普通报告
    def log_message(self, format, *args):
        return

    # do_POST 表示处理模型请求，无显式参数
    # 仅按开关关键词返回工具调用，不能作为真实大模型效果验收
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                raise ValueError("请求大小不合法")
            body = json.loads(self.rfile.read(length))
            messages = body.get("messages", [])
            if messages and messages[-1].get("role") == "tool":
                try:
                    tool = json.loads(messages[-1].get("content", "{}"))
                except ValueError:
                    tool = {}
                content = "模拟开关流程已完成，未检测真实灯光" if tool.get("ok") is True else "设备报告失败，未确认开关成功"
                message = {"role": "assistant", "content": content}
            else:
                text = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "")
                name = next((t["function"]["name"] for t in body.get("tools", []) if t["function"]["name"] in ("light_set", "light.set")), None)
                if name is None:
                    raise ValueError("模型请求未包含墙灯工具")
                on = not any(word in text for word in ("关闭", "关灯", "熄灭"))
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "task4-call-" + uuid.uuid4().hex[:8],
                           "type": "function", "function": {"name": name, "arguments": json.dumps({"on": on})}}]}
            result = {"id": "task4-local-rule", "object": "chat.completion", "model": "task4-rule-fixture",
                      "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]}
            payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            self.send_error(400, "local fixture rejected request")


# free_port 表示选择本机空闲端口，无参数
# 仅绑定127.0.0.1，不向局域网或公网开放测试服务
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# launch 表示启动一个受控子进程，args为独立参数列表
# 日志保存在私有运行目录，进程树由stop_process回收
def launch(args: list[str], *, cwd: Path, env: dict, log_path: Path):
    log = log_path.open("ab")
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=log, stderr=log, **options)
    log.close()
    return process


# stop_process 表示回收本次启动的进程树，process必须来自launch
# Windows用明确PID回收子进程，Linux用本次独立进程组
def stop_process(process) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


# wait_health 表示等待本机网关，base为回环HTTP地址
# 每次等待有总截止时间，不无限挂起
async def wait_health(client, base: str, timeout: float = 25) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            response = await client.get(base + "/api/health")
            if response.status_code == 200:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False


# wait_online 表示等待指定设备上线或离线，online为目标状态
# 始终按设备ID匹配，避免误操作其他在线设备
async def wait_online(client, base: str, jwt: str, device_id: str, online: bool, timeout: float = 20) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(base + "/api/devices", headers={"Authorization": "Bearer " + jwt})
        response.raise_for_status()
        device = next((d for d in response.json() if d["device_id"] == device_id), None)
        if device is not None and bool(device["online"]) is online:
            return True
        await asyncio.sleep(0.2)
    return False


# run_demo 表示独立完成本机闭环，无参数
# 网关保持上游快照，Agent使用错误传播修复版，舵机控制走完整算法的sim后端
async def run_demo() -> dict:
    import httpx

    report = new_report("local-demo", "sim")
    report["scope"] = "本机真实网关、修复版Agent、正式墙灯控制算法的sim后端、规则模型夹具"
    report["agent_source"] = str((resolve_agent_repo() / "agent").relative_to(ROOT))
    report["real_llm_verified"] = False
    report["public_network_verified"] = False
    manifest = json.loads((ROOT / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    report["upstream_commit"] = manifest["commit"]
    run_dir = ROOT / ".runtime" / ("demo-" + uuid.uuid4().hex[:12])
    run_dir.mkdir(parents=True)
    run_dir.chmod(0o700)
    repo = server_repo()
    agent_repo = resolve_agent_repo() / "agent"
    control = run_dir / "fixture-control.json"
    write_json(control, {}, private=True)
    credentials_path = run_dir / "credentials.private.json"
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("PIAGENT_"):
            env.pop(name)
    env.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT),
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
    seed = subprocess.run([sys.executable, "-m", "task4.seed_demo", "--repo", str(repo), "--data-dir", str(run_dir / "data"),
                           "--credentials", str(credentials_path)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    if not check(report, "隔离演示数据库创建", seed.returncode == 0, seed.stderr[-1000:]):
        return report
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    rule_server = ThreadingHTTPServer(("127.0.0.1", 0), RuleHandler)
    rule_thread = threading.Thread(target=rule_server.serve_forever, daemon=True)
    rule_thread.start()
    base = "http://127.0.0.1:" + str(free_port())
    port = base.rsplit(":", 1)[1]
    gateway_env = dict(env, PIAGENT_DATA_DIR=str(run_dir / "data"), PIAGENT_LLM_BASE_URL=f"http://127.0.0.1:{rule_server.server_port}/v1",
                       PIAGENT_LLM_API_KEY="task4-local-only", PIAGENT_LLM_MODEL="task4-rule-fixture")
    gateway = agent = None
    try:
        gateway = launch([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", port, "--no-access-log"],
                         cwd=repo / "server", env=gateway_env, log_path=run_dir / "gateway.log")
        profile = load_profile(ROOT / "profiles/servo-simulation.toml")
        profile["gateway"].update({"base_url": base, "device_id": credentials["device_id"], "access_token_env": "TASK4_DEMO_TOKEN"})
        profile["mcp"]["env"] = {"TASK4_MOCK_CONTROL": str(control)}
        config_path = run_dir / "agent.private.toml"
        config_path.write_text(make_agent_config(profile, credentials["device_token"], device_id=credentials["device_id"], base_url=base), encoding="utf-8")
        config_path.chmod(0o600)
        agent_env = dict(env, PIAGENT_CONFIG=str(config_path), PYTHONPATH=str(agent_repo) + os.pathsep + str(ROOT))
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            if not check(report, "本机网关启动", await wait_health(client, base)):
                return report
            unauthorized = await client.get(base + "/api/devices")
            check(report, "未登录请求被拒绝", unauthorized.status_code == 401)
            for endpoint in ("register", "login"):
                response = await client.post(base + "/api/auth/" + endpoint, json={"username": "task4-test", "password": "unused-local"})
                check(report, "旧入口关闭：" + endpoint, response.status_code == 403)
            jwt = await login(client, base, credentials["access_token"])
            agent = launch([sys.executable, "-m", "pi_agent"], cwd=agent_repo, env=agent_env, log_path=run_dir / "agent.log")
            if not check(report, "真实Agent注册并上报工具", await wait_online(client, base, jwt, credentials["device_id"], True)):
                return report
            prior = os.environ.get("TASK4_DEMO_TOKEN")
            os.environ["TASK4_DEMO_TOKEN"] = credentials["access_token"]
            try:
                gateway_report = await run_gateway(profile, actuate=True, cycles=2, chat=True)
            finally:
                if prior is None:
                    os.environ.pop("TASK4_DEMO_TOKEN", None)
                else:
                    os.environ["TASK4_DEMO_TOKEN"] = prior
            report["checks"].extend(gateway_report["checks"])
            report["device_id"] = credentials["device_id"]
            e2e_env = dict(env, TASK4_ACCESS_TOKEN=credentials["access_token"])
            e2e = await asyncio.to_thread(subprocess.run,
                [sys.executable, str(ROOT / "scripts/e2e_test.py"), "--profile", str(ROOT / "profiles/servo-simulation.toml"),
                 "--base-url", base, "--device-id", credentials["device_id"], "--actuate", "--chat",
                 "--report", str(run_dir / "e2e-report.json")],
                cwd=ROOT, env=e2e_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=100)
            check(report, "新版E2E命令可直接运行", e2e.returncode == 0, e2e.stdout[-600:] + e2e.stderr[-600:])
            if (run_dir / "e2e-report.json").is_file():
                e2e_report = json.loads((run_dir / "e2e-report.json").read_text(encoding="utf-8"))
                check(report, "新版E2E核对聊天方向与设备执行", e2e_report.get("chat_direction_checked") is True
                      and e2e_report.get("chat_device_state_checked") is True and e2e_report.get("hardware_verified") is False)
            status_outer = await manual(client, base, jwt, credentials["device_id"], "devices.status", {})
            _, before, _ = inspect_gateway_result(status_outer)
            rejected = await manual(client, base, jwt, credentials["device_id"], "light.set", {"on": "not-a-boolean"})
            status_outer = await manual(client, base, jwt, credentials["device_id"], "devices.status", {})
            _, after, _ = inspect_gateway_result(status_outer)
            check(report, "非法参数不执行动作", rejected.get("ok") is False and before.get("command_count") == after.get("command_count"))
            write_json(control, {"failure": True}, private=True)
            failed = await manual(client, base, jwt, credentials["device_id"], "light.set", {"on": True})
            success, inner, reason = inspect_gateway_result(failed)
            check(report, "验收器能识别设备失败", not success and inner.get("ok") is False, reason)
            check(report, "修复版Agent正确传播设备失败", failed.get("ok") is False and inner.get("ok") is False, failed)
            chat_failed = await chat_once(base, jwt, credentials["device_id"], "打开寝室灯")
            check(report, "设备故障后聊天不伪报成功", chat_failed.get("ok") is False, chat_failed)
            write_json(control, {}, private=True)
            check(report, "模拟设备不会被当作gpio", before.get("mode") != "gpio" and before.get("lamp_state") == "unknown")
            static = await client.get(base + "/")
            check(report, "上游前端HTML可由网关提供", static.status_code == 200 and "<html" in static.text.lower(), "未做真人浏览器或交互视觉验收")
            stop_process(agent)
            agent = None
            if check(report, "设备离线状态可观察", await wait_online(client, base, jwt, credentials["device_id"], False)):
                offline = await client.post(base + f"/api/devices/{credentials['device_id']}/command", headers={"Authorization": "Bearer " + jwt},
                                            json={"tool": "light.set", "args": {"on": True}})
                check(report, "离线控制返回503", offline.status_code == 503)
            agent = launch([sys.executable, "-m", "pi_agent"], cwd=agent_repo, env=agent_env, log_path=run_dir / "agent-restart.log")
            check(report, "Agent重启后重新上线", await wait_online(client, base, jwt, credentials["device_id"], True))
            final = await manual(client, base, jwt, credentials["device_id"], "devices.status", {})
            ok, state, _ = inspect_gateway_result(final)
            check(report, "模拟设备重启后未自动执行开关", ok and state.get("command_count") == 0)
    except Exception as error:
        check(report, "本机闭环正常结束", False, str(error))
    finally:
        stop_process(agent)
        stop_process(gateway)
        rule_server.shutdown()
        rule_server.server_close()
        rule_thread.join(timeout=3)
        report["processes_stopped"] = all(p is None or p.poll() is not None for p in (agent, gateway))
    return report
