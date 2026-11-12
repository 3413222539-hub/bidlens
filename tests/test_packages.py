"""按标包自查清单的回归测试。

分两层：
1. 已验证事实：手工核对过原件的文档，断言必须完全成立。
2. 全量快照：其余文档记录当前输出，规则改动若改变结果会立刻暴露，
   需要人工确认是新旧哪边错了再更新快照。

运行：.venv\\Scripts\\python.exe -m pytest tests -q
"""

import json
from functools import lru_cache
from pathlib import Path

import pytest

from package_review import package_checklist, read_package_facts, yuan

ROOT = Path(__file__).resolve().parent.parent
IMPORTS = ROOT / "data" / "imports"
SNAPSHOT = Path(__file__).with_name("baseline.json")

# 已人工核对原件的文档，doc_id 前缀 -> 期望事实
VERIFIED = {
    "a465a17b": {
        "name": "北京政务网站与新媒体运营",
        "packages": [1, 2, 3, 4],
        "deposits": {1: "50000.00元", 2: "2500.00元", 3: "2500.00元", 4: "1500.00元"},
        "limits": {1: "487.6482万元", 2: "15.3万元", 3: "14.8万元", 4: "8万元"},
        "deposit_page": 15,
        "limit_page": 14,
        "merged": 4,
        "branches": 2,
    },
    "072efbb6": {
        "name": "苏州科技大学图书采购",
        "packages": [1, 2, 3, 4, 5],
        "deposits": {},
        "waived": True,
        "waiver_page": 3,
        "limits": {1: "75.00万元", 2: "75.00万元", 3: "75.00万元", 4: "75.00万元", 5: "30.00万元"},
        "limit_page": 2,
        "license_page": 7,
    },
    "97ebd11e": {
        "name": "陕西智慧体育设备",
        "packages": [1],
        "deposits": {},
        "waived": True,
        "waiver_page": 9,
    },
    "47e3a561": {
        "name": "宝鸡文理学院图书采购",
        "packages": [1, 2],
        "deposits": {1: "3100元", 2: "2700元"},
        "limits": {1: "160000.00元", 2: "140000.00元"},
        "deposit_page": 14,
        "limit_page": 3,
        "branches": 2,
    },
}

DEPOSIT_TITLE = "投标保证金"
LIMIT_TITLE = "最高限价"
LICENSE_TITLE = "特定资格许可证"


def doc_folder(prefix):
    matches = [path for path in IMPORTS.iterdir() if path.name.startswith(prefix)]
    if not matches:
        pytest.skip(f"缺少样本目录 {prefix}")
    folder = matches[0]
    if not (folder / "document.json").is_file() or not (folder / "risk_report.json").is_file():
        pytest.skip(f"{prefix} 缺少 document.json 或 risk_report.json")
    return folder


@lru_cache(maxsize=None)
def facts_of(prefix):
    """PDF 解析代价高，同一份样本在一次测试会话中只读一次。"""
    return read_package_facts(doc_folder(prefix))


@lru_cache(maxsize=None)
def report_of(prefix):
    folder = doc_folder(prefix)
    return json.loads((folder / "risk_report.json").read_text(encoding="utf-8"))


def build(prefix, package):
    return package_checklist(report_of(prefix), facts_of(prefix), package)


def card_titles(result):
    return [item["title"] for item in result["items"] if item.get("group") == "标包自查"]


def cards_of(result, title):
    return [item for item in result["items"]
            if item.get("group") == "标包自查" and item["title"] == title]


def deposit_amounts(facts):
    """按标包声明的保证金金额。"""
    amounts = {}
    for rule in facts[1]:
        for number, (amount, _waived) in rule["values"].items():
            if amount:
                amounts[number] = amount
    return amounts


def limit_amounts(facts):
    """只取按标包分列的限价；None 键是项目总额（等于各包之和），单独断言。"""
    amounts = {}
    for _sources, values in facts[2]:
        amounts.update({number: amount for number, amount in values.items()
                        if amount and number is not None})
    return amounts


def limit_totals(facts):
    return [amount for _sources, values in facts[2]
            for number, amount in values.items() if number is None and amount]


@pytest.mark.parametrize("prefix", list(VERIFIED))
def test_标包识别(prefix):
    assert sorted(facts_of(prefix)[0]) == VERIFIED[prefix]["packages"]


@pytest.mark.parametrize("prefix", [key for key, value in VERIFIED.items() if value["deposits"]])
def test_保证金金额(prefix):
    assert deposit_amounts(facts_of(prefix)) == VERIFIED[prefix]["deposits"]


@pytest.mark.parametrize("prefix", [key for key, value in VERIFIED.items() if value.get("limits")])
def test_最高限价(prefix):
    assert limit_amounts(facts_of(prefix)) == VERIFIED[prefix]["limits"]


def test_项目总限价与分包之和一致():
    """原文同时写"总限价 330 万"和"五个分包"，两者必须相等。"""
    facts = facts_of("072efbb6")
    assert limit_totals(facts) == ["3300000.00元"]
    amounts = limit_amounts(facts)
    assert sum(yuan(amount) for amount in amounts.values()) == yuan("3300000.00元")


@pytest.mark.parametrize("prefix", [key for key, value in VERIFIED.items() if value.get("waived")])
def test_免收保证金判为不适用(prefix):
    expect = VERIFIED[prefix]
    result = build(prefix, expect["packages"][0])
    card = cards_of(result, DEPOSIT_TITLE)[0]
    assert card["applicability"] == "excluded"
    assert expect["waiver_page"] in {source["page"] for source in card["sources"]}


@pytest.mark.parametrize("prefix", [key for key, value in VERIFIED.items() if value["deposits"]])
def test_保证金卡合并分支与页码(prefix):
    expect = VERIFIED[prefix]
    for package in expect["packages"]:
        result = build(prefix, package)
        card = cards_of(result, DEPOSIT_TITLE)[0]
        assert card["applicability"] == "applies"
        assert expect["deposits"][package] in card["condition"]
        assert len(card.get("branches") or []) == expect["branches"]
        assert expect["deposit_page"] in {source["page"] for source in card["sources"]}
        if expect.get("merged"):
            assert card["merged_count"] == expect["merged"], f"第 {package} 包合并条数变化"


@pytest.mark.parametrize("prefix", [key for key, value in VERIFIED.items() if value.get("limits")])
def test_限价卡按标包取值(prefix):
    expect = VERIFIED[prefix]
    for package in expect["packages"]:
        result = build(prefix, package)
        card = cards_of(result, LIMIT_TITLE)[0]
        assert expect["limits"][package] in card["condition"], f"第 {package} 包限价变化"
        if expect.get("limit_page"):
            assert expect["limit_page"] in {source["page"] for source in card["sources"]}


def test_苏州许可证按标包区分():
    expect = VERIFIED["072efbb6"]
    for package in expect["packages"]:
        result = build("072efbb6", package)
        licenses = cards_of(result, LICENSE_TITLE)
        applies = [card for card in licenses if card["applicability"] == "applies"]
        excluded = [card for card in licenses if card["applicability"] == "excluded"]
        assert len(applies) == 1, f"第 {package} 包应有且仅有一条适用的许可证要求"
        assert len(excluded) == 1
        assert expect["license_page"] in {source["page"] for source in applies[0]["sources"]}
        if package == 5:
            assert "进口" in applies[0]["check"], "第 5 包必须要求出版物进口经营许可证"
            assert "进口" not in excluded[0]["condition"]
        else:
            assert "进口" not in applies[0]["check"]
            assert "进口" in excluded[0]["condition"]


def test_切换标包不串包():
    for prefix, expect in VERIFIED.items():
        if not expect["deposits"]:
            continue
        conditions = {}
        for package in expect["packages"]:
            card = cards_of(build(prefix, package), DEPOSIT_TITLE)[0]
            conditions[package] = card["condition"]
        for package, condition in conditions.items():
            assert expect["deposits"][package] in condition, f"{prefix} 第 {package} 包串包"
        if len(conditions) > 1:
            unique = {value for value in conditions.values()}
            assert len(unique) > 1, f"{prefix} 各标包金额相同，无法区分是否串包"


def test_风险清单与自查卡同源():
    """自查卡必须能追溯到原始风险或原文，不能凭空产生。"""
    for prefix in VERIFIED:
        for package in VERIFIED[prefix]["packages"]:
            result = build(prefix, package)
            for card in result["items"]:
                if card.get("group") != "标包自查":
                    continue
                assert card["sources"], f"{prefix} 第 {package} 包的「{card['title']}」缺少原文引用"
                for source in card["sources"]:
                    assert source["page"] >= 1
                    assert source["text"].strip()


def snapshot_rows():
    prefixes = sorted({folder.name[:8] for folder in IMPORTS.iterdir()
                       if (folder / "document.json").is_file()
                       and (folder / "risk_report.json").is_file()})
    rows = []
    for prefix in prefixes:
        folder = doc_folder(prefix)
        meta = json.loads((folder / "document.json").read_text(encoding="utf-8"))
        facts = facts_of(prefix)
        row = {
            "id": folder.name,
            "name": meta["name"],
            "packages": sorted(facts[0]),
            "methods": sorted({value for _, value in facts[4]}),
            "license_pages": sorted({source["page"] for source, _, _ in facts[3]}),
            "cards": {
                str(package): [[card["title"], card["applicability"]]
                               for card in package_checklist(report_of(prefix), facts, package)["items"]
                               if card.get("group") == "标包自查"]
                for package in sorted(facts[0]) or [None]
            },
            # 无效投标/审查标准条目的适用性分布：改动判定逻辑时这里会先报红
            "applies": {
                str(package): {state: sum(1 for card in package_checklist(report_of(prefix), facts, package)["items"]
                                          if card.get("group") != "标包自查"
                                          and card["applicability"] == state)
                               for state in ("applies", "pending", "excluded")}
                for package in sorted(facts[0]) or [None]
            },
        }
        rows.append(row)
    return rows


def test_全量快照():
    current = {row["id"]: row for row in snapshot_rows()}
    if not SNAPSHOT.is_file():
        SNAPSHOT.write_text(json.dumps(list(current.values()), ensure_ascii=False, indent=1),
                            encoding="utf-8")
        pytest.skip(f"已生成快照 baseline.json（{len(current)} 份），请审阅后提交")
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    missing = [row["id"][:8] for row in expected if row["id"] not in current]
    assert not missing, f"快照中的样本文档已消失：{missing}"
    diffs = []
    for row in expected:
        got = current[row["id"]]
        for field in ("packages", "methods", "license_pages", "cards", "applies"):
            if got[field] != row[field]:
                diffs.append(f"{row['name'][:26]} 的 {field} 变化\n"
                             f"  旧: {row[field]}\n  新: {got[field]}")
    assert not diffs, "按标包自查结果发生变化，确认是有意改动后更新 baseline.json：\n" + "\n".join(diffs)


@pytest.mark.parametrize("prefix", [key for key in VERIFIED])
def test_通用废标条款应判为适用(prefix):
    """无效投标与审查标准是通用规则：除非原文限定范围，否则应对本标包适用。"""
    expect = VERIFIED[prefix]
    for package in expect["packages"]:
        result = build(prefix, package)
        for card in result["items"]:
            if card.get("group") == "标包自查":
                continue
            if card["applicability"] == "pending":
                assert card.get("scope"), \
                    f"{prefix} 第 {package} 包「{card['title']}」判为待核对，但没有说明理由"