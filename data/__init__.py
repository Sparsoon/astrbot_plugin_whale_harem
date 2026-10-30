"""静态数据：物品总表、鱼种表、掉落池。"""

from .items import ITEMS, ItemDef, get_item, name_of, resolve_item

__all__ = ["ITEMS", "ItemDef", "get_item", "name_of", "resolve_item"]