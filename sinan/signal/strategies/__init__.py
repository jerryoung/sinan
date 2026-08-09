"""内置策略包:import 各策略模块即触发其 @register 注册(get_strategy 惰性导入本包)。"""
from . import cadence
from . import combo
from . import dca
from . import donchian
from . import ma_cross
from . import supertrend
from . import tsmom
from . import xsmom
from . import livermore
from . import rebalance
from . import turtle  # noqa: F401
