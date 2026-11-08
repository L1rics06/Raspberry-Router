"""上机调试入口：检查、空载脉宽、交互标定、开关与停止"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import signal
import tomllib
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from .common import ROOT, now_text, write_json
from .pwm_driver import HardwarePWM, SimPWM, LOCK_PATH, doctor, emergency_stop, reset_stop
from .servo_control import ServoControl, calibration_errors, load_settings, save_settings
from .servo_specs import SERVO_MODEL, SERVO_PROFILE, VOLTAGE_MIN_V, VOLTAGE_MAX_V, electrical_errors


# make_parser 建立上机命令，无参数
# 默认真实GPIO，所有会移动的命令必须额外写明actuate
def make_parser():
    parser = argparse.ArgumentParser(description="Pi5寝室开关舵机上机工具")
    parser.add_argument("action", choices=("init", "doctor", "status", "pulse", "calibrate", "on", "off", "cycle", "record-electrical", "stop", "reset-stop"))
    parser.add_argument("--config", type=Path, default=ROOT / "configs/servo.local.toml")
    parser.add_argument("--sim", action="store_true")
    parser.add_argument("--actuate", action="store_true")
    parser.add_argument("--us", type=int, default=1500)
    parser.add_argument("--hold", type=float, default=0.5)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--operator", default="")
    return parser


# initialize 创建或升级SG90现场配置，args含config目标路径
# 旧型号参数先备份再清除标定，已是当前SG90配置时保留现有实测数据
def initialize(args) -> dict:
    calibration = ROOT / "handoff/calibration.local.toml"
    electrical = ROOT / "handoff/electrical.local.json"
    stale = False
    prior_settings = {}
    for path in (args.config, calibration, electrical):
        if path.exists():
            text = path.read_text(encoding="utf-8-sig")
            data = json.loads(text) if path.suffix == ".json" else tomllib.loads(text)
            if not isinstance(data, dict):
                raise ValueError(f"{path}不是配置对象，拒绝自动覆盖")
            if path == args.config:
                prior_settings = data
            if data.get("servo_model") != SERVO_MODEL or data.get("servo_profile") != SERVO_PROFILE:
                stale = True
            nominal = data.get("power_design_voltage_v" if path == electrical else "power_nominal_v")
            if type(nominal) not in (int, float) or nominal != 5:
                stale = True
    guard = LOCK_PATH.open("r+") if platform.system() == "Linux" and LOCK_PATH.exists() else nullcontext(None)
    with guard as lock:
        if lock is not None:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        copies = [(ROOT / "configs/servo.example.toml", args.config),
                  (ROOT / "profiles/hardware.example.toml", ROOT / "profiles/hardware.local.toml"),
                  (ROOT / "handoff/electrical.example.json", electrical),
                  (ROOT / "handoff/calibration.example.toml", calibration)]
        created, backups = [], []
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        for source, target in copies:
            reset = stale and target in (args.config, calibration, electrical)
            if reset and target.exists():
                backup = target.with_name(target.stem + ".before-sg90-" + stamp + target.suffix)
                shutil.copyfile(target, backup)
                backups.append(str(backup))
            if not target.exists() or reset:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                created.append(str(target))
        return {"ok": True, "created": created, "backups": backups,
                "requires_recalibration": stale or prior_settings.get("calibrated") is not True,
                "detail": "SG90 5V配置已准备，旧型号标定不再沿用，没有执行硬件动作"}


# yes 询问一项真实现场结果，text为提示
# 只有明确输入yes才确认，回车不会自动认可
def yes(text: str) -> bool:
    return input(text + " [输入yes确认]: ").strip().lower() == "yes"


# record_electrical 记录操作员实测结果，operator为填写者
# 本函数不会测量电压或电流，没有实测时不能填写通过
def record_electrical(operator: str) -> dict:
    if not operator.strip():
        raise ValueError("请通过--operator填写真实记录人")
    voltage = float(input("实测SG90端电压V（5V方案，Pi输入可能为5.1V）: ").strip())
    if not VOLTAGE_MIN_V <= voltage <= VOLTAGE_MAX_V:
        raise ValueError("本项目5V供电要求实测4.8～5.25V，不能沿用6V电源")
    data = json.loads((ROOT / "handoff/electrical.example.json").read_text(encoding="utf-8"))
    data.update(operator=operator, tested_at=now_text(), measured_voltage_v=voltage,
                servo_gpio_bcm=12, physical_signal_pin=32, servo_model=SERVO_MODEL)
    source = input("供电方式 pi=Pi物理2脚5V，external=独立5V [默认pi]: ").strip().lower() or "pi"
    if source not in ("pi", "external"):
        raise ValueError("供电方式只接受pi或external")
    data.update(power_source="pi5_5v_header" if source == "pi" else "external_5v",
                physical_power_pin=2 if source == "pi" else None)
    for key, text in (("common_ground_checked", "已确认舵机负极与Pi GND共地"),
                      ("signal_compatible", "已完成空载PWM测试并确认舵机识别3.3V信号"),
                      ("power_test_passed", "已验证按压时Pi无欠压/重启、SG90无抖动或持续堵转"),
                      ("pin_conflict_checked", "已确认BCM12物理32脚无其他程序或外设占用")):
        data[key] = yes(text)
    data["approved"] = True
    errors = electrical_errors(data)
    data["approved"] = not errors
    target = write_json(ROOT / "handoff/electrical.local.json", data)
    return {"ok": data["approved"], "record": str(target), "detail": "仅保存操作员填写的记录，工具未自动进行电气测量"}


# calibrate 交互记录三个位置，control为控制器，args含操作者和保存路径
# 每次试探只输出有限时长，标记按压位置后自动退回已标记的home
def calibrate(control: ServoControl, args) -> dict:
    if not args.operator.strip():
        raise ValueError("校准需填写--operator姓名")
    cfg = dict(control.settings)
    if not yes("先脱离开关，准备以1500us中位安装SG90舵盘，已确认可立即停止和断开供电"):
        raise ValueError("尚未准备校准")
    print("SG90从1500us附近开始，不预设0/90/180度与实物行程的对应关系")
    control.pulse_once(cfg["home_us"], 0.5)
    points = {}
    current = cfg["home_us"]
    print("调整舵盘/支架，让中位离开开关，再小步试探")
    print("命令: + / - 每次10us，数字=指定us，home/on/off=记录位置，range 最小 最大=改窗口，save=保存，q=退出")
    while True:
        line = input(f"[{current}us 范围{cfg['min_us']}..{cfg['max_us']}] > ").strip().lower()
        if line == "q":
            return {"ok": False, "detail": "未保存标定"}
        if line in ("home", "on", "off"):
            points[line + "_us"] = current
            print(f"已记录{line}={current}us")
            if line != "home" and "home_us" in points:
                current = points["home_us"]
                control.pulse_once(current, cfg["wait_s"])
            continue
        if line.startswith("range "):
            parts = line.split()
            if len(parts) != 3:
                print("用法: range 1300 1700")
                continue
            try:
                low, high = int(parts[1]), int(parts[2])
                if not 1000 <= low < high <= 2000 or not low <= current <= high:
                    raise ValueError("SG90本包范围需包含当前指令且位于1000..2000")
                candidate = dict(cfg, min_us=low, max_us=high)
                travel = 2 * (high - low) / cfg["step_us"] * cfg["frame_s"] + 3 * cfg["wait_s"]
                if travel > 4.5:
                    raise ValueError("窗口过大，动作会超时")
                if yes("只修改窗口不自动移动，已确认扩大后不会碰撞"):
                    cfg = candidate
                    control.settings = dict(cfg)
            except ValueError as error:
                print(error)
            continue
        if line == "save":
            candidate = dict(cfg, **points, calibrated=True, operator=args.operator, tested_at=now_text())
            candidate["clearance_verified"] = yes("已确认home完全离开开关，两端均有间隙，手动仍可操作")
            candidate["switch_type"] = "single_rocker" if yes("已确认普通单控翘板开关，上下两端能分别开启和关闭") else "unconfirmed"
            errors = calibration_errors(candidate)
            if errors:
                print("不能保存为有效标定：" + "；".join(errors))
                continue
            if control.backend.mode == "sim":
                candidate["fixture"] = True
            save_settings(args.config, candidate)
            handoff = ROOT / "handoff/calibration.local.toml"
            if control.backend.mode == "gpio":
                save_settings(handoff, candidate)
            return {"ok": True, "mode": control.backend.mode, "config": str(args.config), "detail": "已保存操作员标定记录"}
        try:
            target = current + 10 if line == "+" else current - 10 if line == "-" else int(line)
            control.pulse_once(target, cfg["wait_s"])
            current = target
        except ValueError as error:
            print(error)


# main 处理上机命令，无参数
# 没有actuate时禁止动作，异常后清理PWM，不自动重试
def main() -> int:
    args = make_parser().parse_args()
    control = None
    try:
        if args.action == "init":
            result = initialize(args)
        elif args.action == "doctor":
            result = doctor()
        elif args.action == "record-electrical":
            result = record_electrical(args.operator)
        elif args.action in ("stop", "reset-stop"):
            result = emergency_stop() if args.action == "stop" else reset_stop()
        else:
            if args.action != "status" and not args.actuate:
                raise ValueError("此命令会移动舵机，需明确添加--actuate")
            mode = "sim" if args.sim else "gpio"
            cfg = load_settings(args.config, mode)
            control = ServoControl(cfg, SimPWM() if args.sim else HardwarePWM())
            if args.action == "status":
                result = control.status()
            elif args.action == "pulse":
                result = control.pulse_once(args.us, args.hold)
            elif args.action == "calibrate":
                result = calibrate(control, args)
            else:
                if not 1 <= args.cycles <= 10:
                    raise ValueError("cycles只允许1～10")
                results = []
                targets = (True, False) * args.cycles if args.action == "cycle" else (args.action == "on",)
                for on in targets:
                    item = control.set_light(on)
                    results.append(item)
                    if not item["ok"]:
                        break
                result = {"ok": all(item["ok"] for item in results), "mode": mode, "results": results}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") is True else 2
    except KeyboardInterrupt:
        print("已中止，正在停止PWM；机械卡住时请立即断开舵机电源")
        return 130
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    finally:
        if control is not None:
            control.close()


# terminate 把SIGTERM转换为正常清理路径，参数由signal模块传入
def terminate(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
