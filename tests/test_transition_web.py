"""转归矩阵 Web 端到端测试：发布留痕、配对计算、隐藏、资格校验、原子性。"""
import importlib
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ANON_DB_PATH", str(db_file))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, db_file, main


def _csv_bytes(rows, header="受试者号,年龄,邮编,病种"):
    lines = [header] + [",".join(map(str, r)) for r in rows]
    return ("\n".join(lines)).encode("utf-8-sig")


# 12 个同编号受试者，分两个 (年龄,邮编) 组，每组 6 人且含两种病种 -> 可 0 剔除发布
SUBJECTS = [f"S{i:02d}" for i in range(12)]


def _batch_a_rows():
    rows = []
    for i, sid in enumerate(SUBJECTS):
        if i < 6:
            age, zipc = 40, "10000"
        else:
            age, zipc = 50, "20000"
        disease = "糖尿病" if i % 2 == 0 else "高血压"
        rows.append((sid, age, zipc, disease))
    return rows


def _batch_b_rows():
    # 同 12 个编号，病种发生转归；组内仍含两种病种
    later_disease = {
        "S00": "糖尿病", "S01": "糖尿病", "S02": "糖尿病", "S03": "高血压",
        "S04": "糖尿病", "S05": "高血压",  # 组 0：糖尿病 4、高血压 2
        "S06": "肺炎", "S07": "肺炎", "S08": "肺炎", "S09": "肺炎",
        "S10": "肺炎", "S11": "高血压",  # 组 1：肺炎 5、高血压 1
    }
    rows = []
    for i, sid in enumerate(SUBJECTS):
        if i < 6:
            age, zipc = 40, "30000"
        else:
            age, zipc = 50, "40000"
        rows.append((sid, age, zipc, later_disease[sid]))
    return rows


def _publish(c, batch_no, rows):
    r = c.post("/upload",
               files={"file": (f"b{batch_no}.csv", _csv_bytes(rows), "text/csv")},
               follow_redirects=False)
    assert r.status_code == 303
    r = c.post(f"/publish/{batch_no}", data={"confirm": "on"},
               follow_redirects=False)
    assert r.status_code == 303


def test_transition_matrix_full_flow(client):
    c, _, _ = client
    _publish(c, 1, _batch_a_rows())
    _publish(c, 2, _batch_b_rows())

    r = c.get("/transition", params={"earlier": "1", "later": "2"})
    assert r.status_code == 200
    text = r.text
    assert "随访病种转归矩阵" in text
    # 12 个共同编号全部配对
    assert "共同保留并成功配对的受试者：<strong>12</strong>" in text
    # 出现隐藏标记与矩阵结构
    assert "***" in text
    assert "前次病种" in text and "后次病种" in text
    # 任何受试者号都不得出现在转归响应中
    for sid in SUBJECTS:
        assert sid not in text


def test_transition_excludes_subjects_not_in_both_batches(client):
    c, _, _ = client
    a = _batch_a_rows()
    b_full = _batch_b_rows()
    # 后批只保留 10 个编号（S00、S11 不再保留），重组为两个合规组
    kept = [r for r in b_full if r[0] not in {"S00", "S11"}]
    b = []
    for i, (sid, _, _, disease) in enumerate(kept):
        if i < 5:
            age, zipc = 40, "30000"
        else:
            age, zipc = 50, "40000"
        b.append((sid, age, zipc, disease))
    _publish(c, 1, a)
    _publish(c, 2, b)
    r = c.get("/transition", params={"earlier": "1", "later": "2"})
    assert "共同保留并成功配对的受试者：<strong>10</strong>" in r.text
    assert "S00" not in r.text and "S11" not in r.text


def test_no_overlap_shows_hint_not_zero_matrix(client):
    c, _, _ = client
    a = _batch_a_rows()
    # 后批使用完全不同的受试者号，仍为可发布数据
    b = [(f"X{i:02d}", age, zipc, d)
         for i, (_, age, zipc, d) in enumerate(_batch_b_rows())]
    _publish(c, 1, a)
    _publish(c, 2, b)
    r = c.get("/transition", params={"earlier": "1", "later": "2"})
    assert r.status_code == 200
    assert "没有交集" in r.text
    assert "未生成矩阵" in r.text  # 无共同编号时不渲染矩阵表体


def test_unpublished_batch_not_allowed(client):
    c, _, _ = client
    _publish(c, 1, _batch_a_rows())
    # 批次 2 仅上传不发布
    c.post("/upload",
           files={"file": ("b2.csv", _csv_bytes(_batch_b_rows()), "text/csv")})
    r = c.get("/transition", params={"earlier": "1", "later": "2"})
    assert r.status_code == 409
    assert "尚未发布成功" in r.text


def test_rejected_batch_not_allowed(client):
    c, _, _ = client
    _publish(c, 1, _batch_a_rows())
    # 上传一份无解数据（单病种）产生 rejected 批次 #2，并使其后再无 pending
    bad = [(f"R{i}", 40, "10000", "糖尿病") for i in range(3)]
    c.post("/upload",
           files={"file": ("bad.csv", _csv_bytes(bad), "text/csv")})
    r = c.get("/transition", params={"earlier": "1", "later": "2"})
    assert r.status_code == 409
    assert "尚未发布成功" in r.text


def test_legacy_batch_without_mapping_is_uncomputable(client):
    c, db_file, _ = client
    _publish(c, 1, _batch_a_rows())
    _publish(c, 2, _batch_b_rows())

    # 人为构造一个「已发布但无 batch_subjects 记录」的旧批次（模拟升级前数据）
    conn = sqlite3.connect(str(db_file))
    cur = conn.execute(
        "INSERT INTO batches (status, source_filename, row_count, output_csv, "
        "output_filename) VALUES ('published', 'legacy.csv', 3, ?, 'legacy_out.csv')",
        (b"\xef\xbb\xbf\xe5\xb9\xb4\xe9\xbe\x84,\xe9\x82\xae\xe7\xbc\x96,\xe7\x97\x85\xe7\xa7\x8d\n",))
    legacy_id = cur.lastrowid
    conn.commit()
    conn.close()

    # 旧批次仍可下载（原匿名 CSV 下载不受影响）
    assert c.get(f"/download/{legacy_id}").status_code == 200
    # 参与转归计算时明确提示无法计算，不按零人
    r = c.get("/transition", params={"earlier": str(legacy_id), "later": "2"})
    assert r.status_code == 409
    assert "无法计算" in r.text and "不能视为零人" in r.text
    # 首页将其标记为转归不可计算
    assert "旧批次，无对应关系" in c.get("/").text


def test_same_batch_rejected(client):
    c, _, _ = client
    _publish(c, 1, _batch_a_rows())
    r = c.get("/transition", params={"earlier": "1", "later": "1"})
    assert r.status_code == 400
    assert "两个不同的批次" in r.text


def test_missing_params_rejected(client):
    c, _, _ = client
    assert c.get("/transition").status_code == 400
    assert c.get("/transition", params={"earlier": "1"}).status_code == 400


def test_unknown_batch_409(client):
    c, _, _ = client
    r = c.get("/transition", params={"earlier": "99", "later": "100"})
    assert r.status_code == 409 and "不存在" in r.text


def test_publish_atomically_saves_mapping_and_status(client):
    c, db_file, _ = client
    _publish(c, 1, _batch_a_rows())
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    status = conn.execute(
        "SELECT status FROM batches WHERE id = 1").fetchone()["status"]
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM batch_subjects WHERE batch_id = 1").fetchone()["c"]
    conn.close()
    assert status == "published"
    assert n == 12  # 与最终保留行数一致


def test_publish_failure_rolls_back_everything(client, monkeypatch):
    c, db_file, main = client
    c.post("/upload",
           files={"file": ("b1.csv", _csv_bytes(_batch_a_rows()), "text/csv")})

    # 让发布事务在写入对应关系时失败 -> 状态、对应关系、CSV 全部回滚，无残件。
    # sqlite3.Connection 是不可变内置类型，改为替换 database 模块引用的模块对象。
    real_sqlite3 = main.db.sqlite3

    class FailingConnection(real_sqlite3.Connection):
        def executemany(self, sql, params):
            if "INSERT INTO batch_subjects" in sql:
                raise RuntimeError("模拟发布失败")
            return super().executemany(sql, params)

    class FailingSqlite3:
        Row = real_sqlite3.Row

        def connect(self, *a, **kw):
            return FailingConnection(*a, **kw)

    monkeypatch.setattr(main.db, "sqlite3", FailingSqlite3())
    r = c.post("/publish/1", data={"confirm": "on"})
    assert r.status_code == 409

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM batches WHERE id = 1").fetchone()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM batch_subjects WHERE batch_id = 1").fetchone()["c"]
    conn.close()
    assert row["status"] != "published"
    assert n == 0
    assert c.get("/download/1").status_code == 404


def test_anonymous_csv_and_download_unchanged(client):
    c, _, _ = client
    _publish(c, 1, _batch_a_rows())
    d = c.get("/download/1")
    assert d.status_code == 200
    content = d.content.decode("utf-8-sig")
    # 下载内容仍是无受试者号的三列匿名 CSV，字节级结构未受新功能影响
    first = content.splitlines()[0]
    assert first == "年龄,邮编,病种"
    assert "受试者号" not in content
    assert "S00" not in content
    assert d.content.count(b"\n") == 13  # 表头 + 12 行
