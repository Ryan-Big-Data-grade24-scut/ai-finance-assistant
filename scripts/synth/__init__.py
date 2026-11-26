"""虚构账单生成器（stdlib + openpyxl，无新增依赖）。"""
from .engine import Dataset, generate
from .writers import write_all

__all__ = ["Dataset", "generate", "write_all"]
