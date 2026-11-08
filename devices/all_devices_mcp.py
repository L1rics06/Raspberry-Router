"""合并入口，保留上游5个工具并新增寝室墙灯"""
import signal
import sys

from wall_light_mcp import main, terminate


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main([*sys.argv[1:], "--with-existing-devices"]))
