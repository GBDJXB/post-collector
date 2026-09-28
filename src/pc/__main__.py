"""``python -m pc`` 入口，等价于 ``pc`` 命令。"""

from pc.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
