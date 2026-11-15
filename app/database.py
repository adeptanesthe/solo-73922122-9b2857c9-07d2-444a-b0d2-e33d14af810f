"""SQLite 持久化：批次（预览/发布）状态管理与受试者-病种对应关系。

状态机
------
pending  : 已上传，存在可发布的匿名方案，等待确认发布
published: 已确认发布（含可下载 CSV 与最终保留者对应关系），重启后内容不变
rejected : 已上传但无可行匿名方案，不能发布

转归矩阵所需的「最终保留者编号 + 当批病种」与发布状态在**同一个事务**中写入，
因此任何已发布批次必然带有可核验的受试者对应关系；发布失败整体回滚、不留残件。
升级前发布的旧批次在 ``batch_subjects`` 中没有记录，计算转归时按「无法计算」处理。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL CHECK (status IN ('pending','published','rejected')),
    source_filename TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    plan_json       TEXT,
    output_csv      BLOB,
    output_filename TEXT
);

CREATE TABLE IF NOT EXISTS batch_subjects (
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    subject_id TEXT NOT NULL,
    disease    TEXT NOT NULL,
    PRIMARY KEY (batch_id, subject_id)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def insert_pending(conn: sqlite3.Connection, filename: str, row_count: int,
                   plan_json: str) -> int:
    """单事务：使旧 pending 预览失效，再插入新批次（再次上传后旧预览不得发布）。"""
    with conn:
        conn.execute(
            "UPDATE batches SET status = 'rejected' WHERE status = 'pending'")
        cur = conn.execute(
            "INSERT INTO batches (status, source_filename, row_count, plan_json) "
            "VALUES ('pending', ?, ?, ?)",
            (filename, row_count, plan_json),
        )
        return int(cur.lastrowid)


def insert_rejected(conn: sqlite3.Connection, filename: str, row_count: int) -> int:
    with conn:
        conn.execute(
            "UPDATE batches SET status = 'rejected' WHERE status = 'pending'")
        cur = conn.execute(
            "INSERT INTO batches (status, source_filename, row_count) "
            "VALUES ('rejected', ?, ?)",
            (filename, row_count),
        )
        return int(cur.lastrowid)


def get_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def get_latest_pending(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def list_published(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM batches WHERE status = 'published' ORDER BY id DESC"))


def publish_batch(conn: sqlite3.Connection, batch_id: int,
                  output_csv: bytes, output_filename: str,
                  retained: list[tuple[str, str]]) -> None:
    """原子发布：仅当前 pending 批次可发布；在同一事务内写入发布状态、
    匿名 CSV 与最终保留者（受试者号, 病种）对应关系。

    任一步失败抛异常并整体回滚：既不会产生可下载残件，也不会出现
    「已发布但缺少受试者对应关系」的批次。
    """
    with conn:
        row = conn.execute(
            "SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise LookupError("批次不存在")
        if row["status"] != STATUS_PENDING:
            raise RuntimeError("该预览已不可发布（已发布或已被新的上传取代）")
        conn.execute(
            "UPDATE batches SET status = 'published', output_csv = ?, "
            "output_filename = ? WHERE id = ?",
            (output_csv, output_filename, batch_id),
        )
        conn.executemany(
            "INSERT INTO batch_subjects (batch_id, subject_id, disease) "
            "VALUES (?, ?, ?)",
            [(batch_id, sid, disease) for sid, disease in retained],
        )


class TransitionError(Exception):
    """两批无法计算转归矩阵（批次缺失、未发布或旧批次无对应关系）。"""


def load_transition_view(conn: sqlite3.Connection, earlier_id: int,
                         later_id: int
                         ) -> tuple[sqlite3.Row, sqlite3.Row,
                                    dict[str, str], dict[str, str]]:
    """在**同一个已提交视图**（同一连接、同一事务快照）中读取两批的发布状态
    与受试者-病种对应关系。

    - 批次不存在 / 任一批未发布（pending、rejected 即发布失败）→ TransitionError，
      未发布或发布失败的批次不参与计算；
    - 已发布但 ``batch_subjects`` 无记录 → 旧批次缺少可核验对应关系，
      同样 TransitionError（提示无法计算），调用方不得按零人处理。
    返回值只在单次事务内读取，保证两批数据取自同一一致性快照。
    """
    with conn:  # 单事务快照：整个读取过程中两批状态与对应关系保持一致
        def _fetch(batch_id: int) -> tuple[sqlite3.Row, dict[str, str]]:
            batch = conn.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                raise TransitionError(f"批次 #{batch_id} 不存在")
            if batch["status"] != STATUS_PUBLISHED:
                raise TransitionError(
                    f"批次 #{batch_id} 尚未发布成功（当前状态：{batch['status']}），"
                    "未发布或发布失败的批次不参与转归计算")
            mapping = {
                r["subject_id"]: r["disease"]
                for r in conn.execute(
                    "SELECT subject_id, disease FROM batch_subjects "
                    "WHERE batch_id = ?", (batch_id,))
            }
            if not mapping:
                raise TransitionError(
                    f"批次 #{batch_id} 是在对应关系留痕功能上线前发布的旧批次，"
                    "缺少可核验的受试者-病种对应关系，无法计算转归（不能视为零人）")
            return batch, mapping

        earlier, earlier_map = _fetch(earlier_id)
        later, later_map = _fetch(later_id)
        return earlier, later, earlier_map, later_map
