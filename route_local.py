"""Layer 1：本地已装 skill 路由。

扫描用户已安装的 skill（默认 ~/.workbuddy/skills），读取每个 SKILL.md 的
name / description / 正文关键词，对任务文本做轻量打分排序，返回最可能命中的几个。

特性
  * 纯标准库，零网络、零 token、确定性、可测试；
  * 不依赖大模型：只做词重叠 + 技术词加权；
  * 输出 JSON，供 SKILL 工作流或命令行直接消费。

用法
    python route_local.py "把抖店订单汇总成表" [--dir DIR ...] [--top 5] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# 用户级 skill 目录（跨平台：~ 在 Windows 上即 C:\Users\<user>）
DEFAULT_DIRS = [Path(os.path.expanduser("~/.workbuddy/skills"))]

_STOP = {
    # 中英文泛化停用词
    "的", "了", "和", "与", "及", "或", "是", "在", "我", "你", "他", "她", "它",
    "我们", "你们", "他们", "这个", "那个", "一个", "可以", "需要", "进行", "使用",
    "帮我", "请", "如何", "怎么", "什么", "为什么", "一种", "通过", "基于", "针对",
    "今天", "明天", "天气", "怎么样", "你好", "谢谢",
    "the", "and", "for", "with", "this", "that", "from", "you", "your", "use",
    "using", "please", "help", "make", "add", "how", "what", "why", "a", "an",
    "to", "of", "in", "on", "is", "are", "be", "do", "does", "my", "i", "we",
}


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    # 拉丁标识符（技术词 / 文件名 / 域名片段）
    for m in re.findall(r"[A-Za-z][A-Za-z0-9._-]*", text or ""):
        t = m.strip("._-").lower()
        if len(t) >= 2 and t not in _STOP:
            out.add(t)
    # 中文：按 2-gram / 3-gram 切分，保证子词（如「抖店」）能独立命中；
    # 整段都是停用词的 n-gram 跳过（如「的了的」）。
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        for n in (2, 3):
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if g not in _STOP and not all(c in _STOP for c in g):
                    out.add(g)
    return out


def _load_skill(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    name = ""
    desc = ""
    for line in text.splitlines()[:40]:
        s = line.strip()
        if s.startswith("name:"):
            name = s[len("name:"):].strip().strip('"').strip("'")
        elif s.startswith("description:"):
            desc = s[len("description:"):].strip().strip('"').strip("'")
    return {"name": name or path.parent.name, "description": desc,
            "path": str(path.parent), "text": text}


def _score(q_tokens: set[str], skill: dict) -> tuple[float, list[str]]:
    st = _tokens(skill["name"]) | _tokens(skill["description"]) | _tokens(skill["text"][:2000])
    if not st:
        return 0.0, []
    hits = q_tokens & st
    if not hits:
        return 0.0, []
    score = 0.0
    for h in hits:
        score += 2.0 if len(h) >= 4 else 1.0          # 长词（技术词）权重更高
        if h in _tokens(skill["name"]):
            score += 1.5                                # 名称命中加权
    score *= min(1.0, len(hits) / max(1, len(q_tokens)))  # 覆盖率
    return round(score, 2), sorted(hits)


def scan(dirs: list[Path], self_name: str = "multi-agent-audit") -> list[dict]:
    skills: list[dict] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for skill_md in d.rglob("SKILL.md"):
            # 跳过嵌套的 legacy / 非顶层 skill（SKILL.md 应直接位于某 skill 目录下）
            parent = skill_md.parent
            if parent.name in ("legacy", "__pycache__", "node_modules"):
                continue
            if parent.name == self_name:
                continue
            try:
                skills.append(_load_skill(skill_md))
            except Exception:
                continue
    return skills


def route(query: str, dirs: list[Path], top: int = 5,
          self_name: str = "multi-agent-audit") -> dict:
    q_tokens = _tokens(query)
    if not q_tokens:
        return {"query": query, "matches": []}
    skills = scan(dirs, self_name)
    scored = []
    for s in skills:
        sc, hits = _score(q_tokens, s)
        if sc > 0:
            scored.append({"name": s["name"], "description": s["description"],
                           "path": s["path"], "score": sc, "hits": hits})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"query": query, "total_skills": len(skills),
            "matches": scored[:top]}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="本地已装 skill 路由（零 token）")
    ap.add_argument("query", help="任务描述")
    ap.add_argument("--dir", action="append", default=[], help="额外扫描目录（可多次）")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args(argv)
    dirs = list(DEFAULT_DIRS) + [Path(p) for p in args.dir]
    res = route(args.query, dirs, top=args.top)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
