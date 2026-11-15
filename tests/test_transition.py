"""转归矩阵纯函数单元测试：配对、小格隐藏、合计联动隐藏、无交集。"""
from app import transition as tr


def test_matrix_counts_only_common_subjects():
    # S1~S3 两批都在；S4 仅前批、S5 仅后批，均不得参与配对
    earlier = {"S1": "糖尿病", "S2": "糖尿病", "S3": "高血压", "S4": "感冒"}
    later = {"S1": "糖尿病", "S2": "高血压", "S3": "高血压", "S5": "肺炎"}
    m = tr.build_transition_matrix(earlier, later)
    assert m is not None
    assert m["paired_subject_count"] == 3
    # 病种轴按字符串（Unicode 码位）排序，结果确定：糖(U+7CD6) < 高(U+9AD8)
    assert m["row_labels"] == ["糖尿病", "高血压"]
    assert m["col_labels"] == ["糖尿病", "高血压"]
    lookup = {(c["row"], c["col"]): c for c in m["cells"]}
    # 糖尿病->糖尿病 1 人（隐藏）；糖尿病->高血压 1 人（隐藏）；
    # 高血压->高血压 1 人（隐藏）
    assert lookup[("糖尿病", "糖尿病")]["suppressed"] is True
    assert lookup[("糖尿病", "糖尿病")]["value"] == tr.SUPPRESSED
    # 全部格均隐藏 -> 所有合计隐藏，且结构中不含真实小计数
    assert m["any_suppressed"] is True
    assert m["grand_suppressed"] is True
    assert m["grand_total"] == tr.SUPPRESSED
    assert all(rt["suppressed"] for rt in m["row_totals"])
    assert all(ct["suppressed"] for ct in m["col_totals"])
    # 视图模型绝不携带受试者号
    assert "S1" not in str(m) and "S4" not in str(m) and "S5" not in str(m)


def test_visible_cells_when_all_ge_three():
    # 2x2 满网格且四格均 >=3 -> 无隐藏格、行/列/总合计全部可见
    spec = [("糖尿病", "糖尿病", 4), ("糖尿病", "高血压", 3),
            ("高血压", "糖尿病", 5), ("高血压", "高血压", 3)]
    earlier, later = {}, {}
    n = 0
    for r_d, c_d, cnt in spec:
        for _ in range(cnt):
            sid = f"S{n:03d}"
            earlier[sid], later[sid] = r_d, c_d
            n += 1
    m = tr.build_transition_matrix(earlier, later)
    assert m["paired_subject_count"] == 15
    assert m["any_suppressed"] is False
    assert m["grand_suppressed"] is False
    assert m["grand_total"] == 15
    lookup = {(c["row"], c["col"]): c for c in m["cells"]}
    assert set(lookup) == {
        ("糖尿病", "糖尿病"), ("糖尿病", "高血压"),
        ("高血压", "糖尿病"), ("高血压", "高血压")}
    assert all(not c["suppressed"] for c in m["cells"])
    assert lookup[("糖尿病", "糖尿病")]["value"] == 4
    assert lookup[("糖尿病", "高血压")]["value"] == 3
    assert lookup[("高血压", "糖尿病")]["value"] == 5
    assert lookup[("高血压", "高血压")]["value"] == 3
    assert all(not rt["suppressed"] for rt in m["row_totals"])
    assert all(not ct["suppressed"] for ct in m["col_totals"])
    row_totals = {rt["row"]: rt["value"] for rt in m["row_totals"]}
    col_totals = {ct["col"]: ct["value"] for ct in m["col_totals"]}
    assert row_totals == {"糖尿病": 7, "高血压": 8}
    assert col_totals == {"糖尿病": 9, "高血压": 6}


def test_zero_combination_cells_are_suppressed():
    # 出现零人转移组合时，该格隐藏并联动隐藏相关行/列合计与总合计
    spec = [("糖尿病", "糖尿病", 3), ("糖尿病", "高血压", 3),
            ("高血压", "高血压", 3), ("高血压", "肺炎", 3)]
    earlier, later = {}, {}
    n = 0
    for r_d, c_d, cnt in spec:
        for _ in range(cnt):
            sid = f"S{n:03d}"
            earlier[sid], later[sid] = r_d, c_d
            n += 1
    m = tr.build_transition_matrix(earlier, later)
    assert m["any_suppressed"] is True and m["grand_suppressed"] is True
    lookup = {(c["row"], c["col"]): c for c in m["cells"]}
    assert lookup[("糖尿病", "肺炎")]["value"] == tr.SUPPRESSED  # 0 人格
    assert lookup[("高血压", "糖尿病")]["value"] == tr.SUPPRESSED  # 0 人格
    # 两行均含隐藏格 -> 行合计全隐藏
    assert all(rt["suppressed"] for rt in m["row_totals"])
    # 含隐藏格的列（糖尿病、肺炎）隐藏；高血压列两格均满 3 人，合计仍可见
    col_totals = {ct["col"]: ct for ct in m["col_totals"]}
    assert col_totals["糖尿病"]["suppressed"] is True
    assert col_totals["肺炎"]["suppressed"] is True
    assert col_totals["高血压"]["suppressed"] is False
    assert col_totals["高血压"]["value"] == 6
    # 存在隐藏格 -> 总合计隐藏
    assert m["grand_total"] == tr.SUPPRESSED


def test_margins_visible_when_no_suppressed_cell():
    # 2x2 且四个格都 >=3，所有合计均可见且数值正确
    earlier = {}
    later = {}
    # 糖尿病->糖尿病 4、糖尿病->高血压 3、高血压->糖尿病 5、高血压->高血压 3
    spec = [("糖尿病", "糖尿病", 4), ("糖尿病", "高血压", 3),
            ("高血压", "糖尿病", 5), ("高血压", "高血压", 3)]
    n = 0
    for r_d, c_d, cnt in spec:
        for _ in range(cnt):
            sid = f"S{n:03d}"
            earlier[sid] = r_d
            later[sid] = c_d
            n += 1
    m = tr.build_transition_matrix(earlier, later)
    assert m["any_suppressed"] is False
    assert m["grand_total"] == 15
    row_totals = {rt["row"]: rt["value"] for rt in m["row_totals"]}
    col_totals = {ct["col"]: ct["value"] for ct in m["col_totals"]}
    assert row_totals == {"糖尿病": 7, "高血压": 8}
    assert col_totals == {"糖尿病": 9, "高血压": 6}
    assert all(not rt["suppressed"] for rt in m["row_totals"])
    assert all(not ct["suppressed"] for ct in m["col_totals"])


def test_threshold_boundary_two_hidden_three_shown():
    earlier = {f"S{i}": "糖尿病" for i in range(5)}
    later = {f"S{i}": ("高血压" if i < 2 else "糖尿病") for i in range(5)}
    m = tr.build_transition_matrix(earlier, later)
    lookup = {(c["row"], c["col"]): c for c in m["cells"]}
    assert lookup[("糖尿病", "高血压")]["value"] == tr.SUPPRESSED  # 2 人
    assert lookup[("糖尿病", "糖尿病")]["value"] == 3              # 3 人可见
    assert m["any_suppressed"] is True


def test_no_common_subjects_returns_none():
    assert tr.build_transition_matrix({"A": "糖尿病"}, {"B": "高血压"}) is None
    assert tr.build_transition_matrix({}, {"B": "高血压"}) is None
