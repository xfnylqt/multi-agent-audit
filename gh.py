"""GitHub SKILL 查找：为一条任务在 GitHub 上找「对应的 SKILL / 技能仓库」。

设计约束
  * 纯标准库（urllib），无第三方依赖；
  * **零 token**：只打 GitHub 公开搜索 API，绝不经过任何大模型；
  * 网络抖动 / 触发限流时优雅降级，返回 {"ok": False, "error": ...} 而不是抛异常；
  * 内置缓存 + 最小调用间隔，避免把未认证的搜索配额（10 次/分）打爆。

输入：任务文本 + 可选「本地命中的 SKILL 名/分类」作为强信号词。
输出：{"ok", "keywords", "queries", "results":[{full_name, html_url, description, stars, updated}], "error"}

也可作为命令行使用：
    python gh.py "把抖店订单汇总成表" [--skill ecommerce-order-puller] [--limit 6]
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Optional

SEARCH_API = "https://api.github.com/search/repositories"
UA = "multi-agent-audit/2.0 (+https://local.workbuddy.plugin)"
TIMEOUT = 8.0               # 单次 HTTP 超时
BUDGET = 20.0               # 整次查找的总时间预算（含阶梯多查询与重试）
RETRIES = 2                 # 每个查询最多尝试次数（SSL 抖动重试）
MIN_INTERVAL = 1.5          # 两次 GitHub 调用之间的最小间隔（秒）
CACHE_TTL = 300.0           # 5 分钟内同一组词复用结果
MAX_TERMS = 5

# 中文停用/泛化词：这类词进 GitHub 查询只会稀释结果
_CJK_STOP = {
    "帮我", "请帮", "这个", "那个", "一下", "以及", "然后", "生成", "处理", "任务",
    "问题", "可以", "需要", "进行", "有没有", "是不是", "怎么", "如何", "什么",
    "按照", "根据", "我的", "我们", "给我", "现在", "已经", "还是", "代码", "文件",
}
_LATIN_STOP = {
    "the", "and", "for", "with", "this", "that", "from", "you", "your", "are", "was",
    "not", "but", "can", "will", "use", "using", "please", "help", "make", "add",
}

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict]] = {}
_last_call = [0.0]


def _latin_tokens(text: str) -> list[str]:
    out: list[str] = []
    for m in re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{1,24}", text or ""):
        low = m.strip("._-").lower()
        if len(low) < 2 or low in _LATIN_STOP or low in out:
            continue
        out.append(low)
    return out


def _cjk_tokens(text: str) -> list[str]:
    """粗切中文：取 2-4 字片段并按停用词过滤（GitHub 对中文召回本就有限）。"""
    out: list[str] = []
    for m in re.findall(r"[\u4e00-\u9fff]{2,4}", text or ""):
        if m in _CJK_STOP or m in out:
            continue
        out.append(m)
    return out


def extract_keywords(text: str, extra_terms: Optional[Iterable[str]] = None,
                     limit: int = MAX_TERMS) -> list[str]:
    """把任务文本压成少量高区分度检索词。

    英文/技术词（NC、Python、OpenWrt、Flask…）区分度最高，优先；
    中文片段作补充。本地命中的 SKILL 名（如 nc-sales-order-import）拆词后
    放在最前面，它是最强的「对口 SKILL」信号。
    """
    terms: list[str] = []
    for t in (extra_terms or []):
        s = str(t or "").strip()
        if not s:
            continue
        for part in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", s):
            if len(part) >= 2 and part.lower() not in _LATIN_STOP and part not in terms:
                terms.append(part)

    latin = _latin_tokens(text)
    cjk = _cjk_tokens(text)
    # 有技术词时优先技术词，中文只作少量补充，避免噪声
    merged = terms + latin + (cjk if len(latin) < 2 else cjk[:1])
    seen, final = set(), []
    for t in merged:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        final.append(t)
        if len(final) >= limit:
            break
    return final


def _http_search(query: str, per_page: int, timeout: float) -> list[dict]:
    url = SEARCH_API + "?" + urllib.parse.urlencode({
        "q": query, "sort": "stars", "order": "desc", "per_page": str(per_page),
    })
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    items = []
    for it in (data.get("items") or []):
        if not isinstance(it, dict):
            continue
        items.append({
            "full_name": it.get("full_name") or "",
            "html_url": it.get("html_url") or "",
            "description": (it.get("description") or "")[:200],
            "stars": int(it.get("stargazers_count") or 0),
            "updated": (it.get("pushed_at") or "")[:10],
        })
    return items


def _throttle() -> None:
    """两次真实网络调用之间保持最小间隔（未认证搜索配额很紧）。"""
    with _lock:
        delta = time.time() - _last_call[0]
        if delta < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - delta)
        _last_call[0] = time.time()


def _queries(terms: list[str]) -> list[str]:
    """查询降级阶梯：先精准（带 skill），再逐步放宽。

    GitHub 仓库搜索把空格视为 AND，词越多召回越窄 —— 实测带 4~5 个词常返回 0，
    所以阶梯必须一直在，最宽的一级退到单个词，保证「至少能给出候选」。
    """
    ladder = []
    if len(terms) >= 2:
        ladder.append(" ".join(terms[:4]) + " skill")
        ladder.append(" ".join(terms[:2]) + " skill")
    ladder.append(" ".join(terms[:2]))
    ladder.append(terms[0])
    out, seen = [], set()
    for q in ladder:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def search_skills(text: str, extra_terms: Optional[Iterable[str]] = None,
                  limit: int = 6, timeout: float = TIMEOUT,
                  budget: float = BUDGET) -> dict:
    """在 GitHub 上为任务找对应 SKILL。永不抛异常，失败时 ok=False。

    `budget` 是整次查找的墙上时钟上限：网络抖动时宁可少查几档、也不让调用方挂住。
    """
    keywords = extract_keywords(text, extra_terms)
    if not keywords:
        return {"ok": False, "keywords": [], "queries": [], "results": [],
                "error": "任务文本没有可用检索词"}

    queries = _queries(keywords)
    cache_key = (tuple(keywords), limit)
    with _lock:
        hit = _cache.get(cache_key)
        if hit and (time.time() - hit[0]) < CACHE_TTL:
            return dict(hit[1], cached=True)

    t0 = time.time()
    last_err: Optional[str] = None
    got: list[dict] = []
    for q in queries[:3]:                      # 最多 3 档，保护未认证配额
        for attempt in range(RETRIES):
            if time.time() - t0 > budget:
                last_err = last_err or "GitHub 响应超时（网络抖动/限流）"
                break
            try:
                _throttle()
                got = _http_search(q, per_page=max(limit, 6), timeout=timeout)
                last_err = None
                break
            except urllib.error.HTTPError as e:
                # 403/429 多为限流：直接放弃，别继续打
                last_err = f"GitHub 返回 {e.code}（可能触发限流，稍后再试）"
                got = []
                if e.code in (403, 429):
                    break
            except urllib.error.URLError as e:
                # 国内网络访问 github.com 常被墙/极慢：给友好提示而非裸异常，
                # 并结束本轮重试（拦截下重试无意义，避免面板长时间挂起）。
                last_err = "GitHub 暂时无法访问（网络受限），本地路由功能不受影响"
                got = []
                break
            except Exception as e:  # noqa: BLE001  (网络抖动：重试)
                last_err = f"{type(e).__name__}: {e}"
                got = []
                time.sleep(0.5 * (attempt + 1))
        if got:
            break
        if last_err and "限流" in last_err:
            break
        if time.time() - t0 > budget:
            break

    dedup, seen = [], set()
    for it in sorted(got, key=lambda x: x.get("stars", 0), reverse=True):
        key = it.get("full_name")
        if not key or key in seen:
            continue
        seen.add(key)
        dedup.append(it)

    result = {
        "ok": bool(dedup),
        "keywords": keywords,
        "queries": queries,
        "results": dedup[:limit],
        "error": None if dedup else (last_err or "GitHub 上没有找到匹配的仓库"),
        "cached": False,
    }
    with _lock:
        _cache[cache_key] = (time.time(), dict(result))
        if len(_cache) > 64:
            for k in list(_cache)[:-32]:
                _cache.pop(k, None)
    return result


def local_extra_terms(matched_skill: Optional[str], category: Optional[str] = None) -> list[str]:
    """把本地命中的 SKILL 名（nc-sales-order-import）变成检索词，作为强信号。"""
    terms: list[str] = []
    if matched_skill:
        for part in re.split(r"[-_/]", str(matched_skill)):
            if len(part) >= 2:
                terms.append(part)
    if category:
        terms.append(str(category))
    return terms


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="在 GitHub 上为任务找对应 SKILL（零 token）")
    ap.add_argument("text", help="任务描述文本")
    ap.add_argument("--skill", default="", help="本地命中的 SKILL 名（强信号词）")
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--budget", type=float, default=BUDGET)
    args = ap.parse_args(argv)
    extra = local_extra_terms(args.skill) if args.skill else None
    res = search_skills(args.text, extra, limit=args.limit, budget=args.budget)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
