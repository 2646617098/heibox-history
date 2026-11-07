"""PyInstaller 的入口脚本。

单独放一个文件是打包需要：spec 里的 Analysis 只能接收脚本路径，
不能直接写 `heibox.gui:main` 这种入口点。
"""

import sys

from heibox.gui import main

if __name__ == "__main__":
    sys.exit(main())
