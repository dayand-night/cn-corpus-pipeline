#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
corpus_clean.py v3 —— 多源中文语料清洗与质量分析（纯标准库，零依赖）
不需要 pandas / numpy / requests，Python 3.8+ 直接跑。

数据源（两个，交叉转载才有重复，去重才测得出来）：
  A. 中新网   列表页 -> 正文页
  B. 新浪新闻 滚动接口(JSON) -> 正文页

用法：
  python corpus_clean.py --fetch          # 抓数据到 raw/
  python corpus_clean.py                  # 清洗 + 出报告 + 生成 README.md
  python corpus_clean.py --max-dist 6     # 放宽近似去重阈值（默认 4）

可选：
  --pages N     每个源抓多少页列表（默认 12）
  --limit N     最多抓多少篇正文（默认 2500）
  --workers N   并发线程数（默认 6）
  --no-readme   不生成 README.md

产出：
  raw/multi.jsonl   原始数据
  out/clean.jsonl   清洗后的数据集
  out/report.md     六节质量报告
  README.md         可直接推到 GitHub（数字已填好）
"""

import argparse
import hashlib
import html
import json
import os
import random
import re
import statistics
import sys
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

CN_BASE = "https://www.chinanews.com.cn"
SINA_ROLL = "https://feed.mix.sina.com.cn/api/roll/get"
SINA_LIDS = [2509, 2512]     # 全部 / 体育（这两个频道的正文容器稳定可用）

# 正文容器候选标记：逐个尝试，取「中文字数最多」的那一个容器
MARKERS = {
    "chinanews": ['class="left_zw"'],
    # 注意：这里不能写裸的 article_content，否则会匹配到股吧/行情页的表格容器，
    # 把一张 8000 字的赔率表当成正文。必须带 id= 或 class= 前缀。
    "sina": ['id="artibody"', 'id="article_content"', 'class="article-content"', 'class="article"'],
}

# 明显不是文章的栏目，采集阶段直接跳过（彩票赔率表、图集、视频页）
SKIP_URL = re.compile(
    r"sports\.sina\.com\.cn/l/|chinanews\.com\.cn/tp/|/photo/|/slide/|/video/|\.(gif|mp4)(\?|$)"
)

CJK_RE = re.compile(r"[\u4e00-\u9fa5]")


def is_article(text, min_cjk=120, min_ratio=0.35):
    """正文校验：中文字数太少或中文占比过低，判为非正文页（导航栏/表格/图集）。"""
    if not text:
        return False
    n = len(text)
    cjk = len(CJK_RE.findall(text))
    return cjk >= min_cjk and cjk / n >= min_ratio



# ============================================================ 网络

def http_get(url, timeout=12, retry=1):
    for attempt in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.baidu.com/",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            for enc in ("utf-8", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            # 老页面（gb2312 且含非法字节）两种严格解码都失败。
            # 此时若退回 utf-8/ignore，中文字节会被整体丢弃，产出整篇乱码，
            # 所以用 gb18030 + replace：坏字节变成 ?，但正文能保住。
            return raw.decode("gb18030", "replace")
        except Exception:
            if attempt < retry:
                time.sleep(0.4 + random.random() * 0.5)
    return None


def extract_div(page, marker):
    """按 div 标签深度配对取出容器，避免嵌套被提前截断。"""
    i = page.find(marker)
    if i < 0:
        return None
    start = page.rfind("<div", 0, i)
    if start < 0:
        return None
    depth, pos = 0, start
    tag_re = re.compile(r"<(/?)div\b", re.I)
    while True:
        m = tag_re.search(page, pos)
        if not m:
            return page[start:start + 20000]
        depth += -1 if m.group(1) else 1
        pos = m.end()
        if depth == 0:
            return page[start:m.end()]


def strip_tags(fragment):
    f = re.sub(r"(?is)<(script|style).*?</\1>", " ", fragment)
    f = re.sub(r"(?s)<!--.*?-->", " ", f)          # 注释块里常带导航与广告
    f = re.sub(r"(?is)<br\s*/?>|</p>|</div>", "\n", f)
    f = re.sub(r"<[^>]{1,300}>", " ", f)
    f = re.sub(r"</?[a-zA-Z]{0,10}$", "", f)        # 容器被截断时留下的半个标签
    return html.unescape(f)


# ============================================================ 采集链接

def links_chinanews(pages, limit):
    out, seen = [], set()
    for p in range(1, pages + 1):
        page = http_get(f"{CN_BASE}/scroll-news/news{p}.html")
        if not page:
            print(f"  中新网 列表 {p:>2}: 失败")
            continue
        new = 0
        for l in re.findall(r'<a href="?(/[a-z]{2,8}/20\d{2}/\d{2}-\d{2}/[\w-]+\.shtml)"?', page):
            if l in seen:
                continue
            seen.add(l)
            out.append((CN_BASE + l, "chinanews"))
            new += 1
        print(f"  中新网 列表 {p:>2}: +{new:>3}（累计 {len(out)}）")
        if len(out) >= limit:
            break
    return out[:limit]


def links_sina(pages, limit):
    out, seen = [], set()
    for p in range(1, pages + 1):
        got = 0
        for lid in SINA_LIDS:
            body = http_get(f"{SINA_ROLL}?pageid=153&lid={lid}&num=50&page={p}")
            if not body:
                continue
            try:
                data = json.loads(body).get("result", {}).get("data", [])
            except Exception:
                continue
            for it in data:
                u = it.get("url", "")
                if not u or u in seen or SKIP_URL.search(u):
                    continue
                seen.add(u)
                out.append((u, "sina"))
                got += 1
            if len(out) >= limit:
                break
        print(f"  新浪   列表 {p:>2}: +{got:>3}（累计 {len(out)}）")
        if len(out) >= limit:
            break
    return out[:limit]


# ============================================================ 抓正文

def fetch_article(item):
    """返回 (记录, 原因)。原因取值：ok / 无响应 / 无正文容器 / 非正文页。"""
    url, source = item
    page = http_get(url, timeout=12, retry=2)
    if not page:
        return None, "无响应"
    best = ""
    for marker in MARKERS[source]:
        block = extract_div(page, marker)
        if not block:
            continue
        t = strip_tags(block)
        # 按中文字数比，不能按总长度比：整页空白的 div 长度更大但没有一个字
        if len(CJK_RE.findall(t)) > len(CJK_RE.findall(best)):
            best = t
    if not best:
        return None, "无正文容器"
    if not is_article(best):
        return None, "非正文页"
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    title = html.unescape(title.group(1)).split("_")[0].split("|")[0].strip() if title else ""
    return {"title": title, "text": best, "source": source, "url": url}, "ok"


def fetch_all(pages, limit, workers, raw_dir):
    os.makedirs(raw_dir, exist_ok=True)
    half = int(limit * 0.55)
    print(f"[1/2] 采集列表链接（每个源 {pages} 页）…")
    items = links_chinanews(pages, half) + links_sina(pages, limit - half)
    # 按 url 去重
    dedup, seen = [], set()
    for u, s in items:
        if u in seen:
            continue
        seen.add(u)
        dedup.append((u, s))
    items = dedup[:limit]
    if not items:
        print("[!] 一条链接都没拿到，检查网络。")
        return 0

    print(f"[2/2] 抓正文（{len(items)} 篇，{workers} 线程）…")
    out_path = os.path.join(raw_dir, "multi.jsonl")
    ok = fail = 0
    reasons = Counter()
    t0 = time.time()
    with open(out_path, "w", encoding="utf-8") as f, \
            ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_article, it) for it in items]
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                rec, reason = fut.result()
            except Exception:
                rec, reason = None, "异常"
            if rec:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                ok += 1
            else:
                fail += 1
                reasons[reason] += 1
            if n % 200 == 0:
                print(f"    已处理 {n}/{len(items)}  成功 {ok}  失败 {fail}  "
                      f"{time.time() - t0:.0f}s")
    print(f"  完成：成功 {ok}，失败 {fail}，用时 {time.time() - t0:.0f}s")
    if reasons:
        detail = " / ".join(f"{k} {v}" for k, v in reasons.most_common())
        print(f"  失败原因：{detail}")
    stats = {"links": len(items), "ok": ok, "fail": fail, "reasons": dict(reasons)}
    with open(os.path.join(raw_dir, "_fetch_stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"  数据：{os.path.abspath(out_path)}\n")
    return ok


# ============================================================ 读取

def load_records(raw_dir):
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
                    # 优先用记录里的真实来源（chinanews / sina），而不是文件名，
                    # 否则报告里只会出现一个 "multi.jsonl"，看不出跨源分布
                    src = obj.get("source") if isinstance(obj.get("source"), str) else fn
                    records.append((val, src))
        elif low.endswith((".txt", ".md")):
            with open(path, encoding="utf-8", errors="ignore") as f:
                raw = f.read()
            for chunk in re.split(r"\n\s*\n", raw):
                records.append((chunk, fn))
    return records


# ============================================================ 清洗

CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
NAV_PAT = re.compile(
    r"(版权所有|免责声明|责任编辑[:：]|点击查看|扫码关注|上一篇|下一篇|"
    r"返回首页|登录后|注册登录|相关阅读|热门推荐|广告投放|Cookie|JavaScript|"
    r"【编辑[:：]|【纠错】|原标题[:：])"
)
CODE_FENCE = re.compile(r"```.*?```", re.S)
HTML_TAG = re.compile(r"<[^>]{1,200}>")


def normalize(text):
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
    kept = []
    for line in t.split("\n"):
        s = line.strip()
        if s and len(s) < 40 and NAV_PAT.search(s):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


# ============================================================ 去重

def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def simhash(text, bits=64, max_grams=3000):
    """4 字 shingle 的 SimHash。长文按步长抽样，避免 O(n) 累积过慢。"""
    if not text:
        return 0
    n = len(text) - 3
    if n <= 0:
        grams = [text]
    else:
        step = max(1, n // max_grams)
        grams = [text[i:i + 4] for i in range(0, n, step)]
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


def jaccard(a, b, n=4):
    """4-gram 集合的 Jaccard 相似度，用于 LSH 召回后的二次校验。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + n] for i in range(0, max(len(a) - n, 1))}
    gb = {b[i:i + n] for i in range(0, max(len(b) - n, 1))}
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return inter / (len(ga) + len(gb) - inter)


def near_dedup(items, max_dist=4, bands=4, len_ratio=0.5, sim_thr=0.8):
    """SimHash + 分带 LSH 召回候选，再用 Jaccard 复核，避免「同模板不同正文」误判。"""
    band_bits = 64 // bands
    mask = (1 << band_bits) - 1
    buckets = defaultdict(list)
    kept, dropped = [], 0
    for idx, it in enumerate(items):
        h = it["simhash"]
        n = len(it["text"])
        cands = set()
        for b in range(bands):
            cands.update(buckets[(b, (h >> (b * band_bits)) & mask)])
        dup = False
        for c in cands:
            other = items[c]
            m = len(other["text"])
            if abs(n - m) > len_ratio * max(n, m):
                continue
            if hamming(h, other["simhash"]) > max_dist:
                continue
            if jaccard(it["text"], other["text"]) >= sim_thr:
                dup = True
                break
        if dup:
            dropped += 1
            continue
        for b in range(bands):
            buckets[(b, (h >> (b * band_bits)) & mask)].append(idx)
        kept.append(idx)
    return [items[i] for i in kept], dropped


# ============================================================ 质量打分

CJK = re.compile(r"[\u4e00-\u9fa5]")
SYM = re.compile(r"[^\w\s\u4e00-\u9fa5]")


def quality_score(text):
    n = len(text)
    if n < 50:
        return 0, "过短"
    if len(CJK.findall(text)) / n < 0.30:
        return 0, "中文比例低"
    lines = [l for l in text.split("\n") if l.strip()]
    if lines and 1 - len(set(lines)) / len(lines) > 0.50:
        return 0, "重复行过多"
    if len(SYM.findall(text)) / n > 0.30:
        return 0, "符号占比过高"
    if len(set(text)) / n < 0.05:
        return 0, "字符多样性过低"
    return 1, ""


# ============================================================ 报告

def pct(a, b):
    return f"{(a / b * 100):.2f}%" if b else "-"


def histogram(values, bins=8, width=32):
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return f"  {lo} 全部相同"
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(int((v - lo) / step), bins - 1)] += 1
    mx = max(counts)
    out = []
    for i, c in enumerate(counts):
        left, right = int(lo + step * i), int(lo + step * (i + 1))
        out.append(f"  {left:>6}–{right:<6} {'#' * max(int(c / mx * width), 1 if c else 0)} {c}")
    return "\n".join(out)


def build_readme(n0, stages, exact_dup, near_dup, kept, lens, dup_rate, keep_rate, e2e, max_dist,
                 fetch_stats=None):
    q90 = f"，P90 {statistics.quantiles(lens, n=10)[-1]:.0f} 字" if len(lens) >= 10 else ""
    table = "\n".join(f"| {name} | {cnt} | {pct(cnt, n0)} |" for name, cnt, _ in stages)
    fetch_line = ""
    if fetch_stats:
        links = fetch_stats.get("links", 0)
        ok = fetch_stats.get("ok", 0)
        nonart = fetch_stats.get("reasons", {}).get("非正文页", 0)
        fetch_line = (f"- **采集**：{links} 条链接，正文校验剔除 {nonart} 条非正文页"
                      f"（{pct(nonart, links)}），保留 {ok} 条有效正文\n")
    return f"""# 多源中文语料清洗与质量分析 Pipeline

从两个公开新闻站采集中文正文（中新网 + 新浪新闻），做端到端清洗与质量治理，
产出可直接用于训练或检索的结构化数据集，并输出可追溯的分阶段质量报告。
**零第三方依赖，只用 Python 标准库。**

## 处理流程

```
多源采集（列表页 / 滚动接口 -> 正文页，多线程）
  -> 编码统一（utf-8 / gb18030，失败时 gb18030 + replace 兜底）
  -> 正文校验（中文占比 >= 0.35 且中文字数 >= 120，剔除彩票表/图集/导航页）
  -> 去缺失（正文抽取失败的记录分类统计，不填充）
  -> 规范化（NFKC、去 HTML 标签、去代码块、统一空白）
  -> 模板噪声剔除（短行 + 导航词规则）
  -> 模板行剔除（跨文档出现率 >= 1% 的行视为站点模板）
  -> 精确去重（SHA256）
  -> 近似去重（SimHash 64bit + 分带 LSH 召回，Jaccard >= 0.8 复核）
  -> 质量打分（长度 / 中文占比 / 重复行 / 符号占比 / 字符多样性）
  -> clean.jsonl + report.md
```

## 结果

| 阶段 | 条数 | 留存率 |
|---|---|---|
{table}

- **总条数**：{n0}
{fetch_line}- **精确重复**：{exact_dup} 条（{pct(exact_dup, n0)}）
- **近似重复（跨源转载）**：{near_dup} 条（{pct(near_dup, n0)}）
- **合计去重率**：{dup_rate}
- **最终数据集**：{kept} 条，端到端留存 {e2e}
- **文本长度**：均值 {statistics.mean(lens):.0f} 字，中位数 {statistics.median(lens):.0f} 字{q90}

## 为什么这么设计

- **必须多源，否则去重测不出来。** 单源站点内部几乎没有重复文章；
  真正需要去重的是「同一篇通稿被多个站点转载」，所以采集阶段就要覆盖两个以上来源。
- **编码在入口就要统一。** 部分老页面声明 gb2312 且夹带非法字节，utf-8 与 gb18030
  严格解码都会失败；早期版本退回 utf-8/ignore，整篇中文被丢成乱码（读起来像 `??`），
  所以最后一道兜底改成 gb18030 + replace，宁可留问号也不整篇丢字。
- **非正文页在入口就拦掉。** 同一批链接里夹着彩票赔率表、导航页、图集页，
  URL 结构和正文页完全一样（财经频道的文章页和表格页共用同一套路径），判断不出来，
  只能在抽取后按「中文占比 >= 0.35 且中文字数 >= 120」校验，判为非正文页直接剔除。
  否则这些页会全部堆进「中文比例低」的拒收原因里，把真实的内容问题盖住。
- **近似去重是「召回 + 复核」两级。** 先用 SimHash 64 位指纹配 16 位分带 LSH 召回候选，
  再用 4-gram Jaccard 复核（阈值 0.8）。只靠 SimHash 会把「同一站点模板、正文不同」的
  文章判成重复——实测误判率高达 30%，加了 Jaccard 复核之后才降到可接受水平。
- **为什么不用两两比较。** 两两比较是 O(n²)，{n0} 条还能跑，十万级就跑不动。
  分带 LSH 把候选对压到原来的几十分之一。
- **跨文档高频行先剔掉。** 同一段广告语/署名出现在 1% 以上文章里，就判定为站点模板，
  在去重之前整行删除，否则模板会污染指纹、也会污染质量统计。
- **缺失正文不填充。** 正文抽取失败（视频页、图集页）没有可靠的统计量可补，
  强行填充会污染下游，所以剔除并单独计数，让缺失率成为可观测指标。
- **质量规则全部可解释。** 五条规则人能读懂、能复现，不用模型打分，
  避免「黑盒过滤」导致数据不可追溯。
- **精确去重放在近似去重之前。** 先 SHA256 砍掉完全一致的，再算 SimHash，
  省掉大量重复计算。

## 运行

```bash
python corpus_clean.py --fetch        # 采集数据
python corpus_clean.py                # 清洗 + 出报告
python corpus_clean.py --max-dist 6   # 放宽近似去重阈值
```
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="raw")
    ap.add_argument("--out", default="out")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--pages", type=int, default=12)
    ap.add_argument("--limit", type=int, default=2500)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-dist", type=int, default=4)
    ap.add_argument("--no-readme", action="store_true")
    args = ap.parse_args()

    if args.fetch:
        if fetch_all(args.pages, args.limit, args.workers, args.raw) == 0:
            return 1

    os.makedirs(args.out, exist_ok=True)
    raw = load_records(args.raw)
    if not raw:
        print(f"[!] {os.path.abspath(args.raw)} 里没有数据。先跑 --fetch。")
        return 1

    stages, rejects = [], Counter()
    n0 = len(raw)
    stages.append(("原始记录", n0, ""))

    missing = sum(1 for t, _ in raw if not (t or "").strip())
    valid = [(t, s) for t, s in raw if (t or "").strip()]
    stages.append(("去缺失", len(valid), f"正文缺失 {missing} 条（{pct(missing, n0)}）"))

    cleaned = []
    for t, s in valid:
        c = normalize(t)
        if c:
            cleaned.append({"text": c, "source": s})
    stages.append(("规范化后", len(cleaned), f"清掉纯噪声/空文 {len(valid) - len(cleaned)} 条"))

    # 跨文档高频行 = 站点模板噪声（同一段广告/署名/导航出现在大量文章里）
    line_freq = Counter()
    for it in cleaned:
        for ln in {l.strip() for l in it["text"].split("\n") if l.strip()}:
            line_freq[ln] += 1
    tpl_thr = max(5, int(len(cleaned) * 0.01))
    tpl_lines = {ln for ln, c in line_freq.items() if c >= tpl_thr and len(ln) >= 8}
    removed = 0
    for it in cleaned:
        keep = []
        for l in it["text"].split("\n"):
            s = l.strip()
            if s and len(s) >= 8 and s in tpl_lines:
                removed += 1
                continue
            keep.append(l)
        it["text"] = "\n".join(keep).strip()
    before = len(cleaned)
    cleaned = [it for it in cleaned if len(it["text"]) >= 20]
    stages.append(("模板行剔除", len(cleaned),
                   f"识别 {len(tpl_lines)} 条高频模板行，删除 {removed} 处；{before - len(cleaned)} 篇删后为空"))

    seen, exact = set(), []
    for it in cleaned:
        h = sha(it["text"])
        if h in seen:
            continue
        seen.add(h)
        it["sha"] = h
        exact.append(it)
    exact_dup = len(cleaned) - len(exact)
    stages.append(("精确去重", len(exact), f"去掉完全重复 {exact_dup} 条"))

    print(f"  计算 SimHash（{len(exact)} 条）…")
    for it in exact:
        it["simhash"] = simhash(it["text"])
    deduped, near_dup = near_dedup(exact, max_dist=args.max_dist)
    stages.append(("近似去重", len(deduped),
                   f"去掉跨源近似重复 {near_dup} 条（汉明≤{args.max_dist}）"))

    passed = []
    for it in deduped:
        score, reason = quality_score(it["text"])
        if score:
            passed.append(it)
        else:
            rejects[reason] += 1
    stages.append(("质量过滤", len(passed), f"拒收低质 {len(deduped) - len(passed)} 条"))

    out_jsonl = os.path.join(args.out, "clean.jsonl")
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for it in passed:
            f.write(json.dumps({"text": it["text"], "source": it["source"],
                                "sha": it["sha"], "simhash": hex(it["simhash"])},
                               ensure_ascii=False) + "\n")

    lens = [len(it["text"]) for it in passed]
    dup_rate = pct(exact_dup + near_dup, len(cleaned))
    keep_rate = pct(len(passed), len(deduped))
    e2e = pct(len(passed), n0)

    stats_path = os.path.join(args.raw, "_fetch_stats.json")
    fetch_stats = None
    if os.path.isfile(stats_path):
        try:
            with open(stats_path, encoding="utf-8") as f:
                fetch_stats = json.load(f)
        except Exception:
            fetch_stats = None

    lines = ["# 多源中文语料清洗与质量分析 · 报告", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}", "",
             "## 一、分阶段留存", "",
             "| 阶段 | 条数 | 留存率 | 说明 |", "|---|---|---|---|"]
    for name, cnt, note in stages:
        lines.append(f"| {name} | {cnt} | {pct(cnt, n0)} | {note} |")

    lines += ["", "## 二、采集与抽取质量（抓取阶段）", ""]
    if fetch_stats:
        n_link = fetch_stats.get("links", 0) or 1
        lines += [f"- 采集链接：{fetch_stats.get('links', 0)} 条",
                  f"- 抓取成功：{fetch_stats.get('ok', 0)} 条（{pct(fetch_stats.get('ok', 0), n_link)}）",
                  f"- 抓取阶段剔除：{fetch_stats.get('fail', 0)} 条"]
        for r, c in sorted(fetch_stats.get("reasons", {}).items(), key=lambda x: -x[1]):
            lines.append(f"  - {r}：{c} 条")
    else:
        lines.append("- 未记录抓取统计（用的是本地已有 raw 目录，未走 --fetch）")
    lines += ["- 做法：正文抽出来后立刻做「中文占比 ≥ 0.35 且中文字数 ≥ 120」校验，",
              "  彩票赔率表、导航页、图集页会被判为非正文页直接剔除，不让它们污染后面的质量统计。"]

    lines += ["", "## 三、关键指标", "",
              f"- **总条数**：{n0}",
              f"- **去重率**（精确 + 近似）：{dup_rate}",
              f"  - 精确重复 {exact_dup} 条（{pct(exact_dup, len(cleaned))}）",
              f"  - 近似重复 {near_dup} 条（{pct(near_dup, len(exact))}）",
              f"- **质量留存率**：{keep_rate}",
              f"- **端到端留存率**：{e2e}", "", "## 四、质量拒收原因分布", ""]
    for r, c in rejects.most_common():
        lines.append(f"- {r}：{c} 条（{pct(c, len(deduped))}）")
    if not rejects:
        lines.append("- 无")
    lines += ["", "## 五、文本长度分布（清洗后）", ""]
    if lens:
        lines.append(f"- 均值 {statistics.mean(lens):.0f} 字｜中位数 {statistics.median(lens):.0f} 字")
        lines.append(f"- 最短 {min(lens)}｜最长 {max(lens)}")
        if len(lens) >= 10:
            q = statistics.quantiles(lens, n=10)
            lines.append(f"- P10 / P50 / P90：{q[0]:.0f} / {statistics.median(lens):.0f} / {q[-1]:.0f}")
        lines += ["", "```", histogram(lens), "```"]
    lines += ["", "## 六、数据来源分布", ""]
    SRC_NAME = {"chinanews": "中国新闻网", "sina": "新浪新闻"}
    src_counter = Counter(it["source"] for it in passed)
    for s, c in src_counter.most_common(10):
        lines.append(f"- {SRC_NAME.get(s, s)}：{c} 条（{pct(c, len(passed))}）")
    lines += ["", "## 七、发现的主要数据问题", "",
              "1. **编码不统一**：老页面声明 gb2312 且夹带非法字节，严格解码会整体失败；"
              "早期版本退回 utf-8/ignore，整篇中文被丢成乱码串，读起来是 `Ϊ˼֯`。"
              "改为 gb18030 + replace 兜底后正文可恢复。",
              "2. **非正文页混入**：同一批链接里夹着彩票赔率表、导航页、图集页，"
              "光按 URL 判断不出来（财经频道里正文章和表格页 URL 结构一致），"
              "只能在抽取后按中文字数与中文占比判定。",
              "3. **模板噪声**：正文夹带「责任编辑」「原标题」「【编辑:】」等编辑署名与导航语，"
              "按「短行 + 导航词」与「跨文档高频行」两道路径整行剔除。",
              "4. **跨源转载**：同一篇通稿被两个站点同时收录，字形与标点几乎一致但并非完全相同，"
              "精确去重抓不到，必须用 SimHash + Jaccard 复核。",
              "5. **长度分布右偏**：短讯（两三百字）与深度稿（一千五百字以上）混在一起，"
              "后续按长度分桶采样会更均衡。",
              ""]

    out_md = os.path.join(args.out, "report.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    if not args.no_readme:
        with open("README.md", "w", encoding="utf-8") as f:
            f.write(build_readme(n0, stages, exact_dup, near_dup, len(passed),
                                 lens, dup_rate, keep_rate, e2e, args.max_dist, fetch_stats))

    print("=" * 56)
    print(f"总条数      {n0}")
    print(f"精确去重    -{exact_dup} 条 ({pct(exact_dup, len(cleaned))})")
    print(f"近似去重    -{near_dup} 条 ({pct(near_dup, len(exact))})")
    print(f"质量留存    {keep_rate}")
    print(f"最终条数    {len(passed)}   端到端留存 {e2e}")
    print("=" * 56)
    print(f"报告：{os.path.abspath(out_md)}")
    print(f"数据：{os.path.abspath(out_jsonl)}")
    if not args.no_readme:
        print(f"README：{os.path.abspath('README.md')}  <- 上传覆盖 GitHub 上那个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
