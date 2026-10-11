"""multi-agent-audit 2.0 自测：Layer1 本地路由 + Layer2 GitHub 离线降级。

运行：python tests/test_all.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import route_local as rl  # noqa: E402
import gh  # noqa: E402

PASS = 0
FAIL = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] " + name + (("  (" + detail + ")") if detail else ""))
    else:
        FAIL += 1
        print("  [FAIL] " + name + (("  (" + detail + ")") if detail else ""))


def _make_skill(d: Path, name: str, desc: str, body: str = "") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n", encoding="utf-8")


def test_route_local() -> None:
    tmp = Path(tempfile.mkdtemp())
    try:
        _make_skill(tmp / "ecommerce-order-puller", "ecommerce-order-puller",
                    "电商订单拉取与汇总工具，支持抖店、淘宝、微信小店", "")
        _make_skill(tmp / "src-hunter", "src-hunter",
                    "漏洞挖掘与渗透测试 playbook 集合", "")
        _make_skill(tmp / "ponytail", "ponytail", "懒人开发模式最小改动", "")

        res = rl.route("把抖店订单汇总成 Excel 表", [tmp])
        names = [m["name"] for m in res["matches"]]
        ok("本地路由扫描到 3 个 skill", res["total_skills"] == 3, str(res["total_skills"]))
        ok("抖店订单任务命中 ecommerce-order-puller 且排第一",
           names and names[0] == "ecommerce-order-puller", " -> ".join(names))
        ok("命中项带 hits 与 score", bool(res["matches"][0]["hits"])
           and res["matches"][0]["score"] > 0, str(res["matches"][0]["score"]))

        # 不相关任务不应命中（或分数极低）
        res2 = rl.route("今天天气怎么样", [tmp])
        ok("无关任务无强命中", res2["matches"] == [] or res2["matches"][0]["score"] <= 2,
           str(res2["matches"][0]["score"] if res2["matches"] else "none"))

        # 自身应被跳过
        res3 = rl.route("多 agent 协同审计", [tmp], )
        ok("不把自身列为候选", "multi-agent-audit" not in [m["name"] for m in res3["matches"]],
           str([m["name"] for m in res3["matches"]]))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_gh_offline() -> None:
    # 不联网：extract_keywords / _queries 纯函数可用
    kw = gh.extract_keywords("把抖店订单汇总成 Excel 表", gh.local_extra_terms("ecommerce-order-puller"))
    ok("关键词抽取含技术词", "excel" in kw or "ecommerce" in kw, str(kw))
    qs = gh._queries(kw)
    ok("查询阶梯有降级且最宽为单词", len(qs) >= 3 and qs[-1] in kw, str(qs))

    # 垃圾输入在离线（极小 budget）下返回 dict、不抛异常、ok 为布尔
    r = gh.search_skills("的了的", budget=0.001, timeout=0.001)
    ok("垃圾输入离线返回 dict 且 ok 为布尔（不抛异常）",
       isinstance(r, dict) and isinstance(r.get("ok"), bool) and "error" in r,
       str(r.get("error")))

    # 联网失败兜底：把 budget 设极小、timeout 极小，验证永不抛异常
    try:
        r2 = gh.search_skills("python web framework", budget=0.01, timeout=0.01)
        ok("极端 budget 下返回 dict 不抛异常", isinstance(r2, dict) and "ok" in r2,
           str(r2.get("error")))
    except Exception as e:  # noqa: BLE001
        ok("极端 budget 下返回 dict 不抛异常", False, "raised " + repr(e))


def run() -> int:
    global PASS, FAIL
    print("== multi-agent-audit 2.0 self-test ==")
    test_route_local()
    test_gh_offline()
    print(f"\n结果: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
