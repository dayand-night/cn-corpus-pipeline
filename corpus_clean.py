#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
corpus_clean.py —— 中文语料清洗与质量分析（纯标准库，零依赖）
不需要 pandas / numpy / requests，Python 3.8+ 直接跑。

用法：
  1) 准备数据：把任意 .txt / .md / .jsonl 文件放进 ./raw 目录（脚本会自己建）
     也可以先抓一轮 RSS：python corpus_clean.py --fetch
  2) 运行：  python corpus_clean.py
  3) 看结果：out/report.md 和 out/clean.jsonl，控制台会打印三个关键数字

三个关键数字 = 总条数 / 去重率 / 质量留存率，这三个就是面试时
「你处理过多大数据、怎么保证质量」的答案。
"""

import argparse
import hashlib
import html
import json
import os
import re
import statistics
import sys
import unicodedata
import urllib.request
from collections import Counter, defaultdict

# ---------------------------------------------------------------- 数据读取

RSS_SOURCES = [
    "https://www.chinanews.com.cn/rss/scroll-news.xml",
    "https://www.chinanews.com.cn/rss/entertainment.xml",
    "https://www.chinanews.com.cn/rss/tech.xml",
    "https://www.chinanews.com.cn/rss/finance.xml",
]


def fetch_rss(raw_dir, limit_per_feed=400):
    """从公开 RSS 抓一批中文正文，存成 raw/rss_*.jsonl。失败不影响后续步骤。"""
    os.makedirs(raw_dir, exist_ok=True)
    total = 0
    for url in RSS_SOURCES:
        name = url.rstrip("/").split("/")[-1].replace(".xml", "")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode("utf-8", "ignore")
        except Exception as e:
            print(f"  [跳过] {name}: {type(e).__name__} {e}")
            continue

        items = re.findall(r"<item>(.*?)</item>", body, re.S)
        out_path = os.path.join(raw_dir, f"rss_{name}.jsonl")
        n = 0
        with open(out_path, "w", encoding="utf-8") as f:
            for it in items[:limit_per_feed]:
                title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
                desc = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", it, re.S)
                if not desc:
                    continue
                text = html.unescape(re.sub(r"<[^>]+>", "", desc.group(1))).strip()
                if not text:
                    continue
                f.write(json.dumps({
                    "title": html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else "",
                    "text": text,
                    "source": name,
                }, ensure_ascii=False) + "\n")
                n += 1
        total += n
        print(f"  [OK] {name}: {n} 条 -> {out_path}")
    print(f"抓取完成，共 {total} 条。\n")
    return total


def load_records(raw_dir):
    """读取 raw 目录下所有 txt / md / jsonl。返回 [(text, source)]，text 可能为 None（缺失值）。"""
    records = []
    if not os.path.isdir(raw_dir):
        return records
    for fn in sorted(os.listdir(raw_dir)):
        path = os.path.join(raw_dir, fn)
        if not os.path.isfile(path):
            continue
        low = fn.lower()
        if low.endswith(".jsonl"):
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        records.append((line, fn))
                        continue
                    val = None
                    for k in ("text", "content", "completion", "body", "desc"):
                        if isinstance(obj.get(k), str):
                            val = obj[k]
                            break
                    records.append((val, fn))          # val 为 None 即字段缺失
        elif low.endswith((".txt", ".md")):
            with open(path, encoding="utf-8", errors="ignore") as f:
                raw = f.read()
            # txt 按空行切段；md 整篇算一条太长，也按空行切
            for chunk in re.split(r"\n\s*\n", raw):
                records.append((chunk, fn))
    return records


# ---------------------------------------------------------------- 清洗

CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
NAV_PAT = re.compile(
    r"(版权所有|免责声明|责任编辑[:：]|点击查看|扫码关注|上一篇|下一篇|"
    r"返回首页|登录后|注册登录|相关阅读|热门推荐|广告|Cookie|JavaScript)"
)
CODE_FENCE = re.compile(r"```.*?```", re.S)
HTML_TAG = re.compile(r"<[^>]{1,200}>")


def normalize(text):
    """编码统一 + 去噪声。返回清洗后的文本（可能为空串）。"""
    if text is None:
        return None
    t = unicodedata.normalize("NFKC", text)
    t = CODE_FENCE.sub(" ", t)
    t = HTML_TAG.sub(" ", t)
    t = html.unescape(t)
    t = CTRL.sub(" ", t)
    t = re.sub(r"[ \t\u3000]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = t.strip()
    # 去掉模板导航行（整行命中导航词且很短）
    kept = []
    for line in t.split("\n"):
        s = line.strip()
        if s and len(s) < 30 and NAV_PAT.search(s):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


# ---------------------------------------------------------------- 去重

def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def simhash(text, bits=64):
    """字符 4-gram shingle 的 SimHash，纯 hashlib 实现。"""
    if not text:
        return 0
    grams = [text[i:i + 4] for i in range(0, max(len(text) - 3, 1))]
    if not grams:
        grams = [text]
    v = [0] * bits
    for g in grams:
        h = int.from_bytes(hashlib.md5(g.encode("utf-8")).digest()[:8], "big")
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= (1 << i)
    return out


def hamming(a, b):
    return bin(a ^ b).count("1")


def near_dedup(items, max_dist=3, bands=4):
    """SimHash + 分带 LSH：先按 16 位分带分桶，只在桶内两两比较，避免 O(n^2)。"""
    band_bits = 64 // bands
    buckets = defaultdict(list)          # (band_idx, band_value) -> [idx...]
    kept = []
    dropped = 0
    for idx, it in enumerate(items):
        h = it["simhash"]
        cands = set()
        for b in range(bands):
            key = (b, (h >> (b * band_bits)) & ((1 << band_bits) - 1))
            cands.update(buckets[key])
        dup = False
        for c in cands:
            if hamming(h, items[c]["simhash"]) <= max_dist:
                dup = True
                break
        if dup:
            dropped += 1
            continue
        for b in range(bands):
            key = (b, (h >> (b * band_bits)) & ((1 << band_bits) - 1))
            buckets[key].append(idx)
        kept.append(idx)
    return [items[i] for i in kept], dropped


# ---------------------------------------------------------------- 质量打分

CJK = re.compile(r"[\u4e00-\u9fa5]")
SYM = re.compile(r"[^\w\s\u4e00-\u9fa5]")


def quality_score(text):
    """返回 (分数, 命中的拒收原因)。分数 0 = 拒收。"""
    n = len(text)
    if n < 50:
        return 0, "过短"
    cjk_ratio = len(CJK.findall(text)) / n
    if cjk_ratio < 0.30:
        return 0, "中文比例低"
    lines = [l for l in text.split("\n") if l.strip()]
    if lines:
        dup_line_ratio = 1 - len(set(lines)) / len(lines)
        if dup_line_ratio > 0.50:
            return 0, "重复行过多"
    sym_ratio = len(SYM.findall(text)) / n
    if sym_ratio > 0.30:
        return 0, "符号占比过高"
    if len(set(text)) / n < 0.05:
        return 0, "字符多样性过低"
    return 1, ""


# ---------------------------------------------------------------- 报告

def pct(a, b):
    return f"{(a / b * 100):.2f}%" if b else "-"


def histogram(values, bins=8, width=34):
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return f"  {lo} 全部相同"
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        i = min(int((v - lo) / step), bins - 1)
        counts[i] += 1
    mx = max(counts)
    out = []
    for i, c in enumerate(counts):
        left = int(lo + step * i)
        right = int(lo + step * (i + 1))
        bar = "#" * max(int(c / mx * width), 1 if c else 0)
        out.append(f"  {left:>6}–{right:<6} {bar} {c}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="raw")
    ap.add_argument("--out", default="out")
    ap.add_argument("--fetch", action="store_true", help="先抓一轮 RSS 到 raw/")
    ap.add_argument("--max-dist", type=int, default=3, help="SimHash 汉明距离阈值")
    args = ap.parse_args()

    if args.fetch:
        fetch_rss(args.raw)

    os.makedirs(args.out, exist_ok=True)
    raw = load_records(args.raw)
    if not raw:
        print(f"[!] {os.path.abspath(args.raw)} 里没有数据。"
              f"放几个 .txt/.jsonl 进去，或加 --fetch 抓 RSS。")
        return 1

    stats = {"阶段": [], "拒收原因": Counter()}
    n0 = len(raw)
    stats["阶段"].append(("原始记录", n0, ""))

    # 1 缺失值
    missing = sum(1 for t, _ in raw if not (t or "").strip())
    valid = [(t, s) for t, s in raw if (t or "").strip()]
    stats["阶段"].append(("去缺失", len(valid), f"缺失 {missing} 条（{pct(missing, n0)}）"))

    # 2 清洗
    cleaned = []
    for t, s in valid:
        c = normalize(t)
        if c:
            cleaned.append({"text": c, "source": s})
    stats["阶段"].append(("清洗后", len(cleaned), f"清掉空文/纯噪声 {len(valid) - len(cleaned)} 条"))

    # 3 精确去重
    seen, exact = set(), []
    for it in cleaned:
        h = sha(it["text"])
        if h in seen:
            continue
        seen.add(h)
        it["sha"] = h
        exact.append(it)
    stats["阶段"].append(("精确去重", len(exact), f"去掉完全重复 {len(cleaned) - len(exact)} 条"))

    # 4 近似去重
    for it in exact:
        it["simhash"] = simhash(it["text"])
    deduped, near_dropped = near_dedup(exact, max_dist=args.max_dist)
    stats["阶段"].append(("近似去重", len(deduped), f"SimHash 去掉近似重复 {near_dropped} 条（汉明≤{args.max_dist}）"))

    # 5 质量过滤
    passed = []
    for it in deduped:
        score, reason = quality_score(it["text"])
        if score:
            passed.append(it)
        else:
            stats["拒收原因"][reason] += 1
    stats["阶段"].append(("质量过滤", len(passed), f"拒收低质 {len(deduped) - len(passed)} 条"))

    # 输出
    out_jsonl = os.path.join(args.out, "clean.jsonl")
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for it in passed:
            f.write(json.dumps({"text": it["text"], "source": it["source"],
                                "sha": it["sha"], "simhash": hex(it["simhash"])},
                               ensure_ascii=False) + "\n")

    lengths = [len(it["text"]) for it in passed]
    dup_total = n0 - len(passed)

    lines = []
    lines.append("# 中文语料清洗与质量分析 · 报告\n")
    lines.append(f"生成时间：{__import__('datetime').datetime.now():%Y-%m-%d %H:%M}\n")
    lines.append("## 一、分阶段留存\n")
    lines.append("| 阶段 | 条数 | 留存率 | 说明 |")
    lines.append("|---|---|---|---|")
    for name, cnt, note in stats["阶段"]:
        lines.append(f"| {name} | {cnt} | {pct(cnt, n0)} | {note} |")
    lines.append("")
    lines.append("## 二、关键指标\n")
    lines.append(f"- **总条数**：{n0}")
    lines.append(f"- **去重率**（精确 + 近似）：{pct((len(cleaned) - len(exact)) + near_dropped, len(cleaned))}")
    lines.append(f"  - 精确重复 {len(cleaned) - len(exact)} 条（{pct(len(cleaned) - len(exact), len(cleaned))}）")
    lines.append(f"  - 近似重复 {near_dropped} 条（{pct(near_dropped, len(exact))}）")
    lines.append(f"- **质量留存率**：{pct(len(passed), len(deduped))}")
    lines.append(f"- **端到端留存率**：{pct(len(passed), n0)}")
    lines.append("")
    lines.append("## 三、质量拒收原因分布\n")
    if stats["拒收原因"]:
        for r, c in stats["拒收原因"].most_common():
            lines.append(f"- {r}：{c} 条（{pct(c, len(deduped))}）")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 四、文本长度分布（清洗后）\n")
    if lengths:
        lines.append(f"- 均值 {statistics.mean(lengths):.0f} 字｜中位数 {statistics.median(lengths):.0f} 字")
        lines.append(f"- 最短 {min(lengths)}｜最长 {max(lengths)}")
        qs = statistics.quantiles(lengths, n=10) if len(lengths) >= 10 else []
        if qs:
            lines.append(f"- P10/P50/P90：{qs[0]:.0f} / {statistics.median(lengths):.0f} / {qs[-1]:.0f}")
        lines.append("\n```")
        lines.append(histogram(lengths))
        lines.append("```")
    lines.append("")
    lines.append("## 五、数据来源分布\n")
    src = Counter(it["source"] for it in passed)
    for s, c in src.most_common(10):
        lines.append(f"- {s}：{c} 条")
    lines.append("")
    lines.append("## 六、发现的主要数据问题\n")
    lines.append("1. **字段缺失**：部分记录正文为空，按剔除处理并单独统计，不做填充（正文缺失无法用统计量补）。")
    lines.append("2. **模板噪声**：网页正文夹带导航语（责任编辑、扫码关注、相关阅读等），按「短行 + 命中导航词」规则整行剔除。")
    lines.append("3. **重复**：新闻转载导致大量近似重复，精确去重只能去掉完全一致的，近似重复需 SimHash。")
    lines.append("4. **低质**：过短、中文占比低、重复行多、符号堆砌的样本统一拒收并分类计数。")
    lines.append("")

    out_md = os.path.join(args.out, "report.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("=" * 52)
    print(f"总条数      {n0}")
    print(f"精确去重    -{len(cleaned) - len(exact)} 条 ({pct(len(cleaned) - len(exact), len(cleaned))})")
    print(f"近似去重    -{near_dropped} 条 ({pct(near_dropped, len(exact))})")
    print(f"质量留存    {pct(len(passed), len(deduped))}")
    print(f"最终条数    {len(passed)}  端到端留存 {pct(len(passed), n0)}")
    print("=" * 52)
    print(f"报告：{os.path.abspath(out_md)}")
    print(f"数据：{os.path.abspath(out_jsonl)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
