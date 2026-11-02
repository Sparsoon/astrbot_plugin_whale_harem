"""赛博鲸鱼馆 · AstrBot 插件包。

这个 `__init__.py` 只做两件事：
    1. 让目录成为**真正的包**（而不是命名空间包），`main.py` 里的相对导入
       （`from .core.db import ...`）在任何加载方式下都稳定；
    2. 声明版本号，方便脚本/日志引用。

注意：这里**不要** import main / services / ui，
否则 AstrBot 加载插件时会触发重复导入与循环依赖。
"""

__version__ = "1.6.0"
__plugin_name__ = "astrbot_plugin_whale_harem"
