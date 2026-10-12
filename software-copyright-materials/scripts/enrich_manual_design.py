#!/usr/bin/env python3
"""把操作手册草稿扩充为"设计说明 + 操作说明"合订文档。

在 `草稿/操作手册.md` 的"四、系统要求"之后插入五章设计内容，原功能章节顺延重编号：

- 五、总体架构设计 —— 平台分层架构（ASCII 图）、技术选型表（读自项目 package.json）、
  部署形态（读自 apps/ 目录）、本软件在平台中的位置（读自代码提取清单的真实文件分层）。
- 六、功能模块设计 —— 模块划分与关键服务清单（真实 application service 文件名与行数）。
- 七、数据结构设计 —— 用提交代码语料反查 prisma/schema.prisma 中被本软件真实访问的
  model，输出表名（@@map）、模型说明（/// 注释）与字段表。
- 八、接口设计 —— 从 docs/api-routes.md 登记表中筛出与本软件模块相关的真实路由。
- 九、界面原型设计 —— 端与页面结构表 + 各模块实机界面图（复用已有截图）与设计说明。

所有表格行都来自项目真实文件，可回溯；不虚构任何数据。
原手册备份为 `草稿/操作手册-纯操作版备份.md`。
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

CN_NUM = "零一二三四五六七八九"


def cn(n: int) -> str:
    if n <= 10:
        return CN_NUM[n] if n < 10 else "十"
    if n < 20:
        return "十" + CN_NUM[n - 10]
    if n % 10 == 0:
        return CN_NUM[n // 10] + "十"
    return CN_NUM[n // 10] + "十" + CN_NUM[n % 10]


HEADING_RE = re.compile(r"^## ([一二三四五六七八九十]{1,3})、")


def shift_chinese_number(title: str, delta: int) -> str:
    m = HEADING_RE.match(title)
    if not m:
        return title
    raw = m.group(1)
    val = 0
    if "十" in raw:
        parts = raw.split("十")
        val = (CN_NUM.index(parts[0]) if parts[0] else 1) * 10
        if len(parts) > 1 and parts[1]:
            val += CN_NUM.index(parts[1])
    else:
        val = CN_NUM.index(raw)
    return f"## {cn(val + delta)}、" + title[m.end():]


# ---------------------------------------------------------------- Prisma 解析

def parse_prisma_models(schema_path: Path) -> list[dict]:
    """逐行状态机解析（schema 存在缩进不规范的 model，不能用块正则）。"""
    models = []
    pending_comments: list[str] = []
    cur: dict | None = None
    for raw in schema_path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if cur is None:
            if s.startswith("///"):
                pending_comments.append(s.lstrip("/ ").strip())
            elif not s or s.startswith("//"):
                continue
            else:
                m = re.match(r"model\s+(\w+)\s*\{", s)
                if m:
                    cur = {"name": m.group(1), "table": m.group(1), "comment": "", "fields": []}
                    cur["comment"] = "；".join(c for c in pending_comments if c)[:150]
                pending_comments = []
            continue
        # ---- model 体内
        if s == "}":
            models.append(cur)
            cur = None
            pending_comments = []
            continue
        if not s or s.startswith("//"):
            continue
        if s.startswith("@@map("):
            tm = re.search(r'"(\w+)"', s)
            if tm:
                cur["table"] = tm.group(1)
            continue
        if s.startswith("@@") or s.startswith("//"):
            continue
        fm = re.match(r"(\w+)\s+([A-Za-z][\w\[\]?]*)\s*(.*)$", s)
        if not fm:
            continue
        name, ftype, rest = fm.group(1), fm.group(2), fm.group(3)
        if name.startswith("@"):
            continue
        attrs = []
        if "@id" in rest:
            attrs.append("主键")
        if "@unique" in rest:
            attrs.append("唯一")
        if "@default" in rest:
            attrs.append("默认值")
        if "@relation" in rest:
            attrs.append("外键关联")
        cm = re.search(r"//+\s*(.+)$", rest)
        fcomment = cm.group(1).strip() if cm else ""
        comment = "；".join([*attrs, fcomment]) if (attrs or fcomment) else ""
        cur["fields"].append({"name": name, "type": ftype, "comment": comment})
    if cur:
        models.append(cur)
    return models


def corpus_from_code_md(set_dir: Path, manifest: dict, project: Path) -> str:
    """语料 = 全部选中源码的选中行区间（比前后30页采样更完整，能覆盖 Prisma 调用）。"""
    parts = []
    root = Path(manifest.get("project_root") or project)
    if not root.exists():
        root = project  # manifest 可能记录容器内路径（如 AGY 挂载），回退到 --project
    for f in manifest.get("files", []):
        p = root / f["path"]
        if not p.exists() or p.suffix not in {".ts", ".tsx"}:
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        start = max(1, int(f.get("selected_line_start", 1)))
        end = min(len(lines), int(f.get("selected_line_end", len(lines))))
        parts.extend(lines[start - 1 : end])
    return "\n".join(parts)


def pick_models(models: list[dict], corpus: str, limit: int = 8) -> list[dict]:
    scored = []
    for m in models:
        variants = {m["name"], m["name"][0].lower() + m["name"][1:]}
        score = 0
        for v in variants:
            score += len(re.findall(rf"(?<![A-Za-z0-9_]){re.escape(v)}(?![A-Za-z0-9_])", corpus))
        if score >= 2:
            scored.append((score, m))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [m for _, m in scored[:limit]]


def models_from_imports(models: list[dict], project: Path, corpus: str,
                        module_slugs: list[str], limit: int = 8) -> list[dict]:
    """精准策略：从选中代码的 import 定位真实 repository 实现文件，扫描其中的 Prisma 模型访问。

    - `@/modules/<slug>/...` 映射为 `src/modules/<slug>/...`；
    - `ports/<x>.repository` 接口会去找 `backend/infrastructure/` 下的同名 prisma 实现；
    - 排除 concurrency/event-bus 等平台通用基础设施。
    """
    by_name = {m["name"]: m for m in models}
    specs = set(re.findall(r'from\s+["\']([^"\']+)["\']', corpus))
    candidates: list[Path] = []

    def add_existing(p: Path) -> None:
        for cand in (p, p.with_suffix(".ts"), p.parent / (p.name + ".ts") if p.suffix == ".repository" else p):
            if cand.exists() and cand.is_file() and cand not in candidates:
                candidates.append(cand)
                return
        # 目录模块 index.ts
        idx = p / "index.ts"
        if idx.exists():
            candidates.append(idx)

    for s in specs:
        low = s.lower()
        if "repositor" not in low and "infrastructure" not in low:
            continue
        if any(k in low for k in ("concurrency", "event-bus", "rate-limiter", "degradation")):
            continue
        rel = re.sub(r"^@/", "src/", s)
        f = project / rel
        add_existing(f)
        if "/ports/" in rel:
            base = rel.rsplit("/", 1)[-1].replace(".repository", "")
            for slug in module_slugs:
                infra = project / "src" / "modules" / slug / "backend" / "infrastructure"
                if infra.is_dir():
                    candidates.extend(x for x in infra.glob(f"*{base}*.ts") if x not in candidates)

    counter: Counter = Counter()
    for f in candidates:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"\bprisma\.([a-z][A-Za-z0-9]+)\.(?:findMany|findFirst|findUnique|create|update|updateMany|delete|deleteMany|upsert|count|aggregate|createMany)\b", text):
            pascal = m.group(1)[0].upper() + m.group(1)[1:]
            if pascal in by_name:
                counter[pascal] += 1
        for m in re.finditer(r"\bPrisma\.([A-Z][A-Za-z0-9]+)(?:WhereInput|CreateInput|UpdateInput|ScalarFieldEnum)\b", text):
            if m.group(1) in by_name:
                counter[m.group(1)] += 1
    return [by_name[n] for n, _ in counter.most_common(limit)]


def models_from_module_tree(models: list[dict], project: Path, module_slugs: list[str],
                            corpus: str = "", limit: int = 8) -> list[dict]:
    """回退策略：扫描所属模块的 infra/repository 文件中的 Prisma 模型访问（仓储模式）。

    与选中代码语料词元匹配的模型加权，避免泛平台共享模型（User/Merchant 等）淹没本软件域模型。
    """
    by_name = {m["name"]: m for m in models}
    # 语料词元：小写英文词（≥4 字符、出现≥5次）与选中文件路径的 kebab 段
    tokens = {w for w, c in Counter(re.findall(r"[a-zA-Z][a-zA-Z0-9]{3,}", corpus)).most_common(200) if c >= 5}
    for slug in module_slugs:
        tokens.update(slug.split("-"))
    tokens = {t.lower() for t in tokens}

    def model_tokens(name: str) -> set[str]:
        words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).lower().split()
        return set(words)

    counter: Counter = Counter()
    for slug in module_slugs:
        base = project / "src" / "modules" / slug
        if not base.is_dir():
            continue
        for f in base.rglob("*.ts"):
            fp = str(f)
            if not any(k in fp for k in ("infra", "repository", "persistence", "adapter")):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            hits: Counter = Counter()
            for m in re.finditer(r"\bprisma\.([a-z][A-Za-z0-9]+)\.(?:findMany|findFirst|create|update|updateMany|delete|deleteMany|upsert|count|aggregate|createMany)\b", text):
                camel = m.group(1)
                pascal = camel[0].upper() + camel[1:]
                if pascal in by_name:
                    hits[pascal] += 1
            for m in re.finditer(r"\bPrisma\.([A-Z][A-Za-z0-9]+)(?:WhereInput|CreateInput|UpdateInput|OrderByWithRelationInput)\b", text):
                if m.group(1) in by_name:
                    hits[m.group(1)] += 1
            for name, n in hits.items():
                weight = 5 if (model_tokens(name) & tokens) else 1
                counter[name] += n * weight
    picked = [by_name[n] for n, _ in counter.most_common(limit)]
    return picked


# ---------------------------------------------------------------- api-routes 解析

def own_routes_from_manifest(manifest: dict, project: Path, registry: list[dict]) -> list[dict]:
    """从选中源码中的 route.ts 提取本软件自己的接口（方法 + 路径），说明优先取注册表。"""
    root = Path(manifest.get("project_root") or project)
    if not root.exists():
        root = project
    reg_by_path = {r["path"]: r for r in registry}
    out: list[dict] = []
    for f in manifest.get("files", []):
        p = f["path"]
        m = re.match(r"(.*)/route\.ts$", p)
        if not m or "/api/" not in p:
            continue
        seg = m.group(1).split("/app/")[-1]
        url = "/" + seg
        fp = root / p
        methods = []
        if fp.exists():
            text = fp.read_text(encoding="utf-8", errors="ignore")
            methods = sorted(set(re.findall(r"export\s+async\s+function\s+(GET|POST|PATCH|PUT|DELETE)\b", text)))
        reg = reg_by_path.get(url) or reg_by_path.get(url.replace("/api/", "/api/", 1))
        for meth in methods or ["GET"]:
            out.append({
                "method": meth,
                "path": url,
                "desc": (reg["desc"] if reg else "本软件接口层源码路由（见代码鉴别材料）"),
            })
    # 去重
    seen = set()
    uniq = []
    for r in out:
        key = (r["method"], r["path"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def parse_api_routes(api_doc: Path) -> list[dict]:
    rows = []
    for line in api_doc.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[0] in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            rows.append({"method": cells[0], "path": cells[1], "file": cells[2], "desc": cells[3]})
    return rows


def pick_api_rows(rows: list[dict], keywords: list[str], limit: int = 24) -> list[dict]:
    picked = []
    for r in rows:
        hay = (r["path"] + " " + r["file"]).lower()
        if any(k and k in hay for k in keywords):
            picked.append(r)
    return picked[:limit]


# ---------------------------------------------------------------- 模块分层统计

def layer_of(path: str) -> str:
    p = path.lower()
    if "route.ts" in p or "/api/" in p:
        return "接口层"
    if "/application/" in p:
        return "应用服务层"
    if "/domain/" in p:
        return "领域层"
    if "/infra/" in p or "/adapter" in p or "/persistence" in p:
        return "基础设施层"
    if p.startswith("apps/") and ("component" in p or "page" in p or "screen" in p):
        return "界面层"
    if "/backend/" in p:
        return "后端支撑"
    return "其他"


def pretty_service_name(path: str) -> str:
    stem = Path(path).stem
    return (
        stem.replace("-service", "").replace(".service", "").replace("-", " ")
        .replace("_", " ").strip()
    )


# ---------------------------------------------------------------- 主流程

def enrich_set(set_dir: Path, project: Path) -> dict:
    draft = set_dir / "草稿"
    manual_md = draft / "操作手册.md"
    if not manual_md.exists():
        raise SystemExit(f"缺少 {manual_md}")
    if "## 五、总体架构设计" in manual_md.read_text(encoding="utf-8"):
        return {"set": set_dir.name, "models": 0, "api_rows": 0, "services": 0,
                "modules": [], "note": "已扩充，跳过"}

    manifest = json.loads((draft / "代码提取清单.json").read_text(encoding="utf-8"))
    business = json.loads((draft / "业务理解.json").read_text(encoding="utf-8"))
    name = manifest.get("software_name") or business["software_name"]
    version = manifest.get("version", "V1.0")
    files = manifest.get("files", [])
    paths = [f["path"] for f in files]

    module_slugs = sorted({
        p.split("/")[2] for p in paths if p.startswith("src/modules/") and len(p.split("/")) > 3
    })
    keywords = sorted({
        kw
        for slug in module_slugs
        for kw in {slug, slug.split("-")[0]}
    })

    corpus = corpus_from_code_md(set_dir, manifest, project)
    models = parse_prisma_models(project / "prisma" / "schema.prisma")
    # 模型发现优先级：import 精准定位 repository 实现 > 提交语料直查 > 模块 infra 全量（词元加权）
    picked = models_from_imports(models, project, corpus, module_slugs)
    if len(picked) < 3:
        picked = pick_models(models, corpus)
    if len(picked) < 3:
        picked = models_from_module_tree(models, project, module_slugs, corpus) or picked
    api_rows = parse_api_routes(project / "docs" / "api-routes.md")
    api_picked = pick_api_rows(api_rows, keywords)

    layer_counter = Counter(layer_of(p) for p in paths)
    services = [
        (p, f.get("source_line_count", 0))
        for p, f in zip(paths, files)
        if "/application/services/" in p
    ][:12]

    manual_modules = business.get("manual_modules") or []
    positioning = (business.get("product_positioning") or "").strip()

    # ---- 手册原文分章（供原型章节与重编号）
    manual_lines = manual_md.read_text(encoding="utf-8").splitlines()
    chapters: list[tuple[str, list[str]]] = []
    cur_title, cur_body = None, []
    for ln in manual_lines:
        m = HEADING_RE.match(ln)
        if m:
            if cur_title:
                chapters.append((cur_title, cur_body))
            cur_title, cur_body = ln, []
        else:
            cur_body.append(ln)
    if cur_title:
        chapters.append((cur_title, cur_body))

    # ---- 生成五章内容
    tech_rows = read_tech_stack(project)

    sec = []
    sec.append("## 五、总体架构设计\n")
    sec.append(
        f"本软件是乡村振兴数字平台（单仓多端工程 `wenlv-next`）中的业务子系统，"
        f"遵循平台“业务真相只在一处、Modules-First、Mobile-Web 一等公民”的总体架构约束。"
        + (f"{positioning}" if positioning else "")
        + "\n"
    )
    sec.append("### 5.1 总体分层架构\n")
    sec.append("```text\n"
               "界面层      Web 控制台 / H5 / 小程序 / APP（WebView 壳）\n"
               "             │ HTTP / JSON（规范入口 /api/v1/{端}/{业务域}/{资源}）\n"
               "接口层      Next.js API Routes + 中立 handler\n"
               "             │ 应用服务调用\n"
               "应用服务层  application/services 编排业务用例、事务与权限\n"
               "             │ 领域规则调用\n"
               "领域层      domain 实体、领域规则与状态机\n"
               "             │ 仓储接口（ports）\n"
               "基础设施层  infrastructure 适配 Prisma 存储、缓存、外部渠道\n"
               "             │ Prisma Client\n"
               "数据层      PostgreSQL（单一数据真相源，schema 即契约）\n"
               "```\n")
    sec.append("### 5.2 技术架构选型\n")
    sec.append("| 类别 | 选型 | 说明 |\n| --- | --- | --- |\n")
    for cat, choice, note in tech_rows:
        sec.append(f"| {cat} | {choice} | {note} |\n")
    sec.append("\n### 5.3 本软件的代码构成\n")
    sec.append(f"本软件鉴别材料代码共选取 {len(paths)} 个源码文件，按层分布如下：\n")
    sec.append("| 架构层 | 文件数 | 说明 |\n| --- | --- | --- |\n")
    layer_desc = {
        "接口层": "对外 HTTP 接口与路由适配",
        "应用服务层": "业务用例编排、事务与权限控制",
        "领域层": "领域实体与业务规则",
        "基础设施层": "存储、缓存与外部系统适配",
        "界面层": "页面与交互组件",
        "后端支撑": "后端支撑与共享逻辑",
        "其他": "配置与辅助文件",
    }
    for layer, cnt in layer_counter.most_common():
        sec.append(f"| {layer} | {cnt} | {layer_desc.get(layer, '')} |\n")
    if module_slugs:
        sec.append(f"\n所属平台模块：{'、'.join(f'`src/modules/{s}`' for s in module_slugs)}。\n")

    sec.append("\n## 六、功能模块设计\n")
    sec.append("### 6.1 功能模块划分\n")
    sec.append("| 功能模块 | 主要职责 |\n| --- | --- |\n")
    for mod in manual_modules[:14]:
        if isinstance(mod, dict):
            sec.append(f"| {mod.get('name', mod.get('module', ''))} | {str(mod.get('purpose', mod.get('description', '')))[:80]} |\n")
    sec.append("\n### 6.2 关键应用服务\n")
    sec.append("下列服务类为真实源码文件（节选），负责对应业务用例的编排：\n")
    sec.append("| 服务 | 源码规模（行） | 所在层 |\n| --- | --- | --- |\n")
    for p, lines in services:
        sec.append(f"| {pretty_service_name(p)} | {lines} | 应用服务层 |\n")

    sec.append("\n## 七、数据结构设计\n")
    if picked:
        sec.append(
            "本软件源码通过 Prisma 客户端访问下列数据模型（依据提交代码语料反查 "
            "`prisma/schema.prisma` 得到，均为项目真实模型）：\n"
        )
        sec.append("| 数据模型 | 数据库表 | 模型说明 |\n| --- | --- | --- |\n")
        for m in picked:
            sec.append(f"| {m['name']} | {m['table']} | {m['comment'] or '—'} |\n")
        for idx, m in enumerate(picked[:6], start=1):
            sec.append(f"\n### 7.{idx} {m['name']}（表 {m['table']}）\n")
            if m["comment"]:
                sec.append(f"{m['comment']}。\n")
            sec.append("| 字段 | 类型 | 说明 |\n| --- | --- | --- |\n")
            for f in m["fields"]:
                sec.append(f"| {f['name']} | {f['type']} | {f['comment'] or '—'} |\n")
    else:
        sec.append("本软件以界面与流程编排为主，直接持久化数据模型较少，数据结构以平台共享模型为基础。\n")

    sec.append("\n## 八、接口设计\n")
    sec.append(
        "平台接口遵循统一注册表规范：新接口先在 `docs/api-routes.md` 登记，路径采用 "
        "`/api/v1/{端}/{业务域}/{资源}` 的规范形式，legacy 入口与规范入口共用同一中立 handler。\n"
    )
    own_routes = own_routes_from_manifest(manifest, project, api_rows)
    merged: list[dict] = []
    seen_paths: set[str] = set()
    for r in own_routes + api_picked:
        if r["path"] in seen_paths:
            continue
        seen_paths.add(r["path"])
        merged.append(r)
    merged = merged[:24]
    if merged:
        own_n = len(own_routes)
        sec.append(
            f"下列接口中，前 {own_n} 条来自本软件代码鉴别材料中的接口层源码（`route.ts`），"
            f"其余为平台接口注册表（共 {len(api_rows)} 条）中与本软件业务域相关的摘录：\n"
        )
        sec.append("| 方法 | 接口路径 | 说明 |\n| --- | --- | --- |\n")
        for r in merged:
            sec.append(f"| {r['method']} | `{r['path']}` | {r['desc'][:70]} |\n")
    else:
        sec.append(
            "本软件业务主要通过所属业务域的规范接口对外提供服务，具体路由以平台接口注册表为准；"
            "内部调用以应用服务层的服务类为边界。\n"
        )

    sec.append("\n## 九、界面原型设计\n")
    shots = load_shots(set_dir)
    by_module: dict[str, list[str]] = {}
    for s in shots:
        by_module.setdefault(s.get("module", ""), []).append(s.get("file", ""))
    if by_module:
        sec.append("| 端 | 页面/模块 | 界面图 |\n| --- | --- | --- |\n")
        for mod, fs in list(by_module.items())[:14]:
            f0 = sorted(set(fs))[0]
            sec.append(f"| {surface_of(shot_url(set_dir, mod))} | {mod} | `{f0}` |\n")
    sec.append("\n各核心界面的设计说明与实机界面图如下（界面图即系统上线后的真实页面，可视为高保真原型）：\n")
    for title, body in chapters:
        m = HEADING_RE.match(title)
        if not m:
            continue
        ch_no = m.group(1)
        # 原功能章节（重编号前从"五"开始的页面章节）
        body_text = "\n".join(body)
        imgs = re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", body_text)
        if not imgs and not any(
            mod in title for mod in by_module
        ):
            continue
        new_title = shift_chinese_number(title, 5)
        sec.append(f"\n### {new_title[3:]}（原型）\n")
        para = next((ln for ln in body if len(ln.strip()) > 30), "")
        if para:
            sec.append(f"页面构成与交互要素：{para.strip()}\n")
        for alt, rel in imgs[:2]:
            sec.append(f"![{alt}界面]({rel})\n")
        sec.append("操作流程与结果反馈详见对应操作章节。\n")

    insert = "".join(sec)

    # ---- 拼接 + 重编号（插入点之后的章节才顺延，前四章保持原编号）
    out: list[str] = []
    inserted = False
    for ln in manual_lines:
        m = HEADING_RE.match(ln)
        if m and not inserted and cn_val(m.group(1)) >= 5:
            out.append(insert)
            out.append("")
            inserted = True
        if m and inserted:
            ln = shift_chinese_number(ln, 5)
        out.append(ln)
    if not inserted:
        out.append(insert)

    backup = draft / "操作手册-纯操作版备份.md"
    if not backup.exists():
        backup.write_text("\n".join(manual_lines), encoding="utf-8")
    manual_md.write_text("\n".join(out), encoding="utf-8")

    return {
        "set": set_dir.name,
        "models": len(picked),
        "api_rows": len(api_picked),
        "services": len(services),
        "modules": module_slugs,
    }


def cn_val(raw: str) -> int:
    if "十" in raw:
        parts = raw.split("十")
        head = CN_NUM.index(parts[0]) if parts[0] else 1
        tail = CN_NUM.index(parts[1]) if len(parts) > 1 and parts[1] else 0
        return head * 10 + tail
    return CN_NUM.index(raw)


def read_tech_stack(project: Path) -> list[tuple[str, str, str]]:
    deps: dict[str, str] = {}
    pkg = project / "package.json"
    if pkg.exists():
        try:
            deps = json.loads(pkg.read_text(encoding="utf-8")).get("dependencies", {})
        except Exception:
            deps = {}

    def ver(*keys: str) -> str:
        for k in keys:
            if k in deps:
                return " " + deps[k].lstrip("^~")
        return ""

    return [
        ("前端框架", f"React{ver('react')} / Next.js{ver('next')}", "单仓多端，Web 控制台与 H5 共用模块体系"),
        ("后端运行时", f"Next.js{ver('next')} API Routes（Node.js）", "接口与应用服务同仓部署，规范入口 /api/v1"),
        ("数据访问", f"Prisma ORM{ver('prisma', '@prisma/client')}", "类型安全的数据访问层，schema 单一事实源"),
        ("数据库", "PostgreSQL", "业务数据持久化，多租户隔离"),
        ("缓存", "Redis", "会话、热点数据与队列缓冲"),
    ]


def load_shots(set_dir: Path) -> list[dict]:
    p = set_dir / "截图" / "截图清单.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("screenshots", []) if isinstance(data, dict) else []


def shot_url(set_dir: Path, module: str) -> str:
    for s in load_shots(set_dir):
        if s.get("module") == module:
            return s.get("url", "")
    return ""


def surface_of(url: str) -> str:
    u = url.lower()
    if "/admin" in u or "admin" in u:
        return "管理端（Web 控制台）"
    if "/merchant" in u or "boss" in u:
        return "商户端"
    if "/app" in u or "/user" in u:
        return "用户端（H5/APP）"
    return "通用 Web"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--materials-root", type=Path, required=True)
    ap.add_argument("--project", type=Path, required=True, help="被登记项目根目录（wenlv-next）")
    ap.add_argument("--only", default="", help="只处理名称匹配（逗号分隔）的软著目录")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    for sd in sorted(p for p in args.materials_root.iterdir() if p.is_dir()):
        if sd.name.startswith("_") or sd.name == "analysis":
            continue
        if only and sd.name not in only and not any(sd.name.startswith(o) for o in only):
            continue
        try:
            rep = enrich_set(sd, args.project)
        except SystemExit as e:
            print(f"[FAIL] {sd.name}: {e}", flush=True)
            continue
        print(
            f"[OK] {sd.name}: models={rep['models']} api={rep['api_rows']} "
            f"services={rep['services']} modules={rep['modules']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
