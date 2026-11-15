"""两次随访批次间的病种转归矩阵（纯函数核心，不接触数据库）。

输入为同一已提交视图中读出的两个「受试者号 -> 病种」映射：

* 仅按**两批实际共同保留的受试者号**配对（内连接），任一批次缺失的编号不参与；
* 统计 ``前次病种 -> 后次病种`` 的人数矩阵；
* 人数少于 3 的格只显示隐藏标记 ``***``，不暴露真实人数；
* 为避免用行/列合计反推出被隐藏格的人数，凡含隐藏格的行合计、列合计一并隐藏；
  只要存在任一隐藏格，总合计也隐藏；
* 本模块只返回病种标签与显示值，**绝不返回受试者号**。

旧批次缺少可核验对应关系、批次未发布等情形由数据库层拦截并报「无法计算」，
调用方不得把缺失对应关系当作零人。
"""
from __future__ import annotations

from typing import Optional

SUPPRESSED = "***"
MIN_CELL_COUNT = 3


def build_transition_matrix(earlier: dict[str, str],
                            later: dict[str, str]) -> Optional[dict]:
    """构建转归矩阵视图模型。

    返回 ``None`` 表示两批保留者没有共同受试者号（无法配对）；
    否则返回可直接渲染的结构，所有人数均为已脱敏后的显示值（整数或隐藏标记）。
    """
    common = sorted(set(earlier) & set(later))
    if not common:
        return None

    # 病种轴按名称排序，保证同一数据多次计算结果确定
    row_labels = sorted({earlier[sid] for sid in common})
    col_labels = sorted({later[sid] for sid in common})

    counts: dict[tuple[str, str], int] = {}
    for sid in common:
        key = (earlier[sid], later[sid])
        counts[key] = counts.get(key, 0) + 1

    def visible(total: int) -> bool:
        return total >= MIN_CELL_COUNT

    suppressed_rows: set[str] = set()
    suppressed_cols: set[str] = set()
    any_suppressed = False

    cells: list[dict] = []
    for r_label in row_labels:
        for c_label in col_labels:
            total = counts.get((r_label, c_label), 0)
            if visible(total):
                cells.append({"row": r_label, "col": c_label,
                              "value": total, "suppressed": False})
            else:
                any_suppressed = True
                suppressed_rows.add(r_label)
                suppressed_cols.add(c_label)
                cells.append({"row": r_label, "col": c_label,
                              "value": SUPPRESSED, "suppressed": True})

    # 行合计：该行含任一隐藏格则连同行合计一起隐藏，否则可由各可见格相加反推，安全
    row_totals = []
    for r_label in row_labels:
        if r_label in suppressed_rows:
            row_totals.append({"row": r_label, "value": SUPPRESSED,
                               "suppressed": True})
        else:
            total = sum(counts.get((r_label, c), 0) for c in col_labels)
            row_totals.append({"row": r_label, "value": total,
                               "suppressed": False})

    col_totals = []
    for c_label in col_labels:
        if c_label in suppressed_cols:
            col_totals.append({"col": c_label, "value": SUPPRESSED,
                               "suppressed": True})
        else:
            total = sum(counts.get((r, c_label), 0) for r in row_labels)
            col_totals.append({"col": c_label, "value": total,
                               "suppressed": False})

    # 总合计：存在任何隐藏格时均可被用于反推，整体隐藏
    grand = (SUPPRESSED if any_suppressed else len(common))

    return {
        "row_labels": row_labels,
        "col_labels": col_labels,
        "cells": cells,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "grand_total": grand,
        "grand_suppressed": any_suppressed,
        "paired_subject_count": len(common),
        "any_suppressed": any_suppressed,
        "min_cell": MIN_CELL_COUNT,
    }
