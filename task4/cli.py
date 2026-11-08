"""任务4统一入口，默认只读，主动动作需要显式参数"""
from __future__ import annotations

import argparse
import asyncio
import getpass
from pathlib import Path

from .common import ROOT, check, exit_code, new_report, save_report
from .profile import load_profile, preflight


# parser 表示建立命令行参数，无参数
# 配置通过文件传入，真实凭证从环境变量读取
def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="任务4：部署、模拟联调和验收")
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("preflight", "mcp-check", "gateway-check", "prepare-agent"):
        item = commands.add_parser(name)
        item.add_argument("--profile", type=Path, required=True)
        item.add_argument("--report", type=Path)
        if name in ("mcp-check", "gateway-check"):
            item.add_argument("--actuate", action="store_true", help="允许开关动作，真机执行前必须有人现场观察")
            item.add_argument("--cycles", type=int, default=1)
        if name == "gateway-check":
            item.add_argument("--chat", action="store_true", help="额外测试两条聊天指令，会执行开关动作")
        if name == "prepare-agent":
            item.add_argument("--user", default=getpass.getuser())
            item.add_argument("--output", type=Path, default=ROOT / ".runtime/generated")
            item.add_argument("--repo", type=Path)
    for name in ("demo", "audit-upstream"):
        commands.add_parser(name).add_argument("--report", type=Path)
    evidence = commands.add_parser("evidence")
    evidence.add_argument("--technical-report", type=Path, required=True)
    evidence.add_argument("--evidence", type=Path, required=True)
    evidence.add_argument("--report", type=Path)
    return cli


# dispatch 表示执行一个命令，args为已解析参数
# 真机MCP启动前先做平台与交接检查，缺失条件时直接返回
async def dispatch(args) -> dict:
    if args.command == "demo":
        from .local_demo import run_demo
        return await run_demo()
    if args.command == "audit-upstream":
        from .audit import audit_upstream
        return audit_upstream()
    if args.command == "evidence":
        from .evidence import collect_evidence
        return collect_evidence(args.technical_report, args.evidence)
    profile = load_profile(args.profile)
    if args.command == "preflight":
        return preflight(profile)
    if args.command == "mcp-check":
        gate = preflight(profile)
        if any(c["status"] != "PASS" for c in gate["checks"]):
            return gate
        from .mcp_checks import run_mcp
        return await run_mcp(profile, actuate=args.actuate, cycles=args.cycles)
    if args.command == "gateway-check":
        from .gateway_checks import run_gateway
        return await run_gateway(profile, actuate=args.actuate, cycles=args.cycles, chat=args.chat)
    if args.command == "prepare-agent":
        from .deploy import prepare_agent
        return prepare_agent(profile, output=args.output, user=args.user, repo=args.repo)
    raise ValueError("未知命令")


# main 表示控制台入口，无参数
# 失败报告会脱敏，Ctrl+C直接中止，不重发舵机动作
def main() -> int:
    args = parser().parse_args()
    try:
        report = asyncio.run(dispatch(args))
    except KeyboardInterrupt:
        print("检查已中止，请现场确认机构位置")
        return 130
    except Exception as error:
        report = new_report(args.command, "gpio" if args.command in ("prepare-agent", "evidence") else "sim")
        check(report, "命令正常完成", False, str(error))
    save_report(report, args.report)
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
