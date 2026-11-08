"""分工4网关验收入口，默认只读，明确指定设备后才允许按压"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from task4.common import check, exit_code, new_report, save_report
from task4.gateway_checks import run_gateway
from task4.profile import load_profile


# parser 创建E2E参数，无参数
# 凭证从TASK4_ACCESS_TOKEN读取，不放在命令参数或源码中
def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="SG90墙灯E2E，使用light.set布尔接口，不自动选择设备")
    cli.add_argument("--profile", type=Path, default=ROOT / "profiles/hardware.local.toml")
    cli.add_argument("--base-url", help="覆盖profile中的网关HTTP或HTTPS地址")
    cli.add_argument("--device-id", help="明确指定自己的设备，不选择第一台在线设备")
    cli.add_argument("--actuate", action="store_true", help="允许真实或模拟开关动作，默认只读")
    cli.add_argument("--cycles", type=int, default=1)
    cli.add_argument("--chat", action="store_true", help="额外测试开灯、关灯聊天，需同时使用--actuate")
    cli.add_argument("--report", type=Path)
    return cli


# main 执行一次验收，无参数
# 失败不重发物理动作，报告内外层ok及开关方向都需一致
def main() -> int:
    args = parser().parse_args()
    report = new_report("gateway", "gpio")
    try:
        profile = load_profile(args.profile)
        profile["gateway"]["access_token_env"] = "TASK4_ACCESS_TOKEN"
        if args.base_url:
            profile["gateway"]["base_url"] = args.base_url
        if args.device_id:
            profile["gateway"]["device_id"] = args.device_id
        report = asyncio.run(run_gateway(profile, actuate=args.actuate, cycles=args.cycles, chat=args.chat))
    except KeyboardInterrupt:
        print("验收已中止，请现场确认舵机与开关位置")
        return 130
    except Exception as error:
        check(report, "E2E配置与运行正常", False, str(error))
    save_report(report, args.report)
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
