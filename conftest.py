"""测试引导：src 布局下让 ``import seismic_assoc`` 在 pytest 中无需安装。

不影响包本身的任何运行时契约；已安装到虚拟环境时这里只是把 src 提前。
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / 'src'
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
