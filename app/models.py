"""核心领域模型：颜色、阶段、事件、状态。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class Color(str, Enum):
    WHITE = "white"
    GRAY = "gray"
    BLACK = "black"


class Phase(str, Enum):
    """回收周期阶段。

    IDLE    空闲（清扫完成或尚未启动）
    MARKING 标记进行中（灰队列可能非空）
    READY   标记完成（灰队列已清空，等待清扫）
    """

    IDLE = "idle"
    MARKING = "marking"
    READY = "ready"


@dataclass(frozen=True)
class Event:
    """以稳定标识持久化的事件。"""

    id: str
    type: str
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerEntry:
    """事件账本行：accepted=False 的行仅用于回放原始拒因，不驱动状态。"""

    event_id: str
    type: str
    payload: dict
    accepted: bool
    reason: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class State:
    """GC 状态。所有变换都返回新的 State（纯函数）。"""

    universe: tuple[str, ...]               # 演练声明的全部对象
    alive: frozenset[str]                   # 尚未被回收的对象
    edges: Mapping[str, frozenset[str]]     # 存活对象之间的有向边
    roots: frozenset[str]                   # 当前根集合
    colors: Mapping[str, Color]             # 每个存活对象的颜色
    queue: tuple[str, ...]                  # 稳定排序的灰队列
    phase: Phase = Phase.IDLE
