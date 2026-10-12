#!/usr/bin/env python3
"""从既有 DOCX/草稿生成软著鉴别材料 PDF（源程序与文档的前30页/后30页）。

输入：每个软著目录下的 草稿/代码-前30页.md、代码-后30页.md（或 代码-全部.md）
      以及 预览资料（或 正式资料）里的 操作手册 DOCX。
输出：同目录 PDF/ 下的 源程序(前30页)/(后30页)/(前30页+后30页).pdf、
      文档(前30页)/(后30页)/(前30页+后30页).pdf（文档不足 60 页时输出 文档(全部).pdf）。

代码 PDF 的页数由硬约束保证：每行先按 90 显示列折行（全角 2 列、tab 展开 4 空格），
再每 50 行插入显式分页符，行距 14pt 精确值，50×14pt=700pt < A4 可用 728pt，
与排版引擎无关地锁定每页 50 行、恰好 30 页。

DOCX→PDF 通过 Docker 内的 LibreOffice（镜像默认 softcopy-pdf）完成，
宿主机的 Noto CJK 字体以只读方式挂载进容器，保证中文渲染。

仅依赖 Python 标准库；pdfinfo/gs/pdfunite 由系统提供。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

DEFAULT_IMAGE = "softcopy-pdf"
DEFAULT_FONT_ASCII = "DejaVu Sans Mono"
DEFAULT_FONT_EA = "Noto Serif CJK SC"
MAX_CODE_COLUMNS = 90
LINES_PER_PAGE = 50
FRONT_BACK_PAGES = 30
FRONT_BACK_LINES = FRONT_BACK_PAGES * LINES_PER_PAGE
FULL_SUBMIT_PAGES = 60


def display_width(text: str) -> int:
    return sum(_col_width(c) for c in text)


def _col_width(char: str) -> int:
    """W/F 记 2 列；A（ambiguous，如 ─ → ≥ …）在 CJK 字体下常渲染为全角，
    PDF 阶段按 2 列保守处理，避免横向换行破坏每页 50 行的硬约束。"""
    return 2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1


def wrap_display_line(line: str, max_columns: int = MAX_CODE_COLUMNS) -> list[str]:
    """与 extract_code_material.wrap_display_line 语义一致（A 类更保守）：不丢字、不依赖 Word 换行。"""
    expanded = line.expandtabs(4)
    if not expanded:
        return [""]
    segments: list[str] = []
    current: list[str] = []
    width = 0
    for char in expanded:
        cw = _col_width(char)
        if current and width + cw > max_columns:
            segments.append("".join(current))
            current, width = [], 0
        current.append(char)
        width += cw
    if current:
        segments.append("".join(current))
    return segments


def parse_code_md(md_path: Path) -> tuple[str, str, list[str]]:
    """读取 代码-*.md，返回 (软件名称, 版本号, 代码行列表)，保留原始顺序。"""
    text = md_path.read_text(encoding="utf-8")
    name_m = re.search(r"^软件名称：(.+)$", text, re.M)
    ver_m = re.search(r"^版本号：(.+)$", text, re.M)
    name = name_m.group(1).strip() if name_m else ""
    version = ver_m.group(1).strip() if ver_m else ""
    lines: list[str] = []
    in_block = False
    for line in text.splitlines():
        if not in_block:
            if line.strip().startswith("```"):
                in_block = True
            continue
        if line.strip().startswith("```"):
            in_block = False
            continue
        lines.append(line.rstrip())
    return name, version, lines


PARA_PPR = (
    '<w:pPr><w:spacing w:before="0" w:after="0" w:lineRule="exact" w:line="280"/>'
    "{pbb}</w:pPr>"
)
RUN_BREAK_PAGE = '<w:r><w:rPr><w:color w:val="000000"/></w:rPr><w:br w:type="page"/></w:r>'


def paragraph_xml(text: str, *, ascii_font: str, ea_font: str, break_after: bool) -> str:
    ppr = PARA_PPR.format(pbb="")
    rpr = (
        f'<w:rPr><w:rFonts w:ascii="{ascii_font}" w:hAnsi="{ascii_font}"'
        f' w:eastAsia="{ea_font}"/><w:color w:val="000000"/><w:sz w:val="14"/>'
        f'<w:szCs w:val="14"/></w:rPr>'
    )
    safe = " " if not text.strip() else text
    run = f'<w:r>{rpr}<w:t xml:space="preserve">{escape(safe)}</w:t></w:r>'
    tail = RUN_BREAK_PAGE if break_after else ""
    return f"<w:p>{ppr}{run}{tail}</w:p>"


def build_code_docx(
    template_docx: Path,
    lines: list[str],
    out_path: Path,
    *,
    ascii_font: str,
    ea_font: str,
    lines_per_page: int = LINES_PER_PAGE,
) -> int:
    """以既有代码 DOCX 为模板（保留页眉页脚/节属性/样式），重写正文为折行+硬分页。"""
    with zipfile.ZipFile(template_docx) as zf:
        names = zf.namelist()
        contents = {n: zf.read(n) for n in names}
    doc = contents["word/document.xml"].decode("utf-8")
    sect = re.search(r"<w:sectPr.*?</w:sectPr>", doc, re.S)
    if not sect:
        raise SystemExit(f"模板缺少 sectPr: {template_docx}")

    paras: list[str] = []
    page_line_count = 0
    for raw in lines:
        for folded in wrap_display_line(raw):
            page_line_count += 1
            break_after = page_line_count % lines_per_page == 0
            paras.append(paragraph_xml(folded, ascii_font=ascii_font, ea_font=ea_font, break_after=break_after))
    body = "".join(paras) + sect.group(0)
    contents["word/document.xml"] = (
        doc[: doc.index("<w:body>") + len("<w:body>")] + body + "</w:body></w:document>"
    ).encode("utf-8")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in names:
            zf.writestr(n, contents[n])
    total_lines = sum(len(wrap_display_line(r)) for r in lines)
    return (total_lines + lines_per_page - 1) // lines_per_page


def ensure_image(image: str, tar: Path | None) -> None:
    """镜像被磁盘清理误删时，自动从持久化 tar 恢复（docker load），无需网络。"""
    check = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if check.returncode == 0:
        return
    if tar and tar.exists():
        subprocess.run(["docker", "load", "-i", str(tar)], check=True, capture_output=True)
        loaded = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
        if loaded.returncode == 0:
            return
        raise SystemExit(f"已从 {tar} 加载，但镜像 {image} 仍不可用")
    raise SystemExit(
        f"镜像 {image} 不存在（可能被 docker 磁盘清理移除），且备用 tar 不存在（{tar}）。"
        f"请先重建：docker build -t {image} <SKILL_DIR>/docker"
    )


def docker_convert(image: str, files: list[Path], host_fonts: Path, workdir: Path,
                   image_tar: Path | None = None) -> list[Path]:
    """在容器内逐个把 DOCX 转成 PDF，输出到 workdir/out/。"""
    ensure_image(image, image_tar)
    indir = workdir / "in"
    outdir = workdir / "out"
    indir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    targets = []
    for f in files:
        dst = indir / f.name
        if not dst.exists():
            shutil.copy2(f, dst)
        targets.append(dst)
    script = (
        'set -e; for f in /data/in/*.docx; do '
        'soffice --headless --convert-to pdf --outdir /data/out "$f" >/dev/null 2>&1 || exit 9; done'
    )
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{workdir}:/data",
        "-v", f"{host_fonts}:/usr/share/fonts/opentype/noto:ro",
        "--entrypoint", "sh", image, "-c", script,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"DOCX→PDF 转换失败: {proc.stderr.strip()[:500]}")
    pdfs = []
    for t in targets:
        pdf = outdir / (t.stem + ".pdf")
        if not pdf.exists():
            raise SystemExit(f"容器未产出 PDF: {t.name}")
        pdfs.append(pdf)
    return pdfs


def pdf_page_count(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    m = re.search(r"^Pages:\s+(\d+)$", out, re.M)
    if not m:
        raise SystemExit(f"pdfinfo 未返回页数: {pdf}")
    return int(m.group(1))


def extract_pages(src: Path, first: int, last: int, out: Path) -> None:
    subprocess.run(
        ["gs", "-dBATCH", "-dNOPAUSE", "-q", "-sDEVICE=pdfwrite",
         f"-sPageList={first}-{last}", f"-sOutputFile={out}", str(src)],
        check=True, capture_output=True,
    )


def find_one(base: Path, pattern: str) -> Path | None:
    matches = sorted(base.glob(pattern))
    return matches[0] if matches else None


def process_set(set_dir: Path, args: argparse.Namespace) -> dict:
    draft = set_dir / "草稿"
    preview = set_dir / "预览资料"
    if not preview.is_dir():
        preview = set_dir / "正式资料"
    if not draft.is_dir() or not preview.is_dir():
        return {"set": set_dir.name, "status": "skipped", "reason": "缺少 草稿/ 或 预览资料/"}

    report: dict = {"set": set_dir.name, "outputs": [], "checks": [], "status": "ok"}

    with tempfile.TemporaryDirectory(prefix=f"pdf-{set_dir.name}-") as td:
        workdir = Path(td)
        jobs: list[Path] = []          # 待转换 docx
        job_meta: dict[str, dict] = {}  # stem -> 预期页数等

        # ---- 源程序鉴别材料 ----
        front_md = draft / "代码-前30页.md"
        back_md = draft / "代码-后30页.md"
        all_md = draft / "代码-全部.md"
        front_tpl = find_one(preview, "*-代码(前30页).docx") or find_one(preview, "*代码*前30*.docx")
        back_tpl = find_one(preview, "*-代码(后30页).docx") or find_one(preview, "*代码*后30*.docx")
        all_tpl = find_one(preview, "*-代码(全部).docx") or find_one(preview, "*代码*全部*.docx")

        def prepare_code(md: Path, tpl: Path | None, mode: str) -> None:
            name, version, lines = parse_code_md(md)
            if not lines:
                raise SystemExit(f"{md} 中未找到代码行")
            base_name = name or preview.parent.name
            # 先整体折行，再按物理行切首/尾，保证恰好 30 页 × 50 行
            folded: list[str] = []
            for raw in lines:
                folded.extend(wrap_display_line(raw))
            if mode == "front":
                use = folded[:FRONT_BACK_LINES]
                if len(use) < FRONT_BACK_LINES:
                    raise SystemExit(
                        f"{md.name} 折行后仅 {len(use)} 行，不足前30页材料（1500 行）"
                    )
                label = "前30页"
            elif mode == "back":
                use = folded[-FRONT_BACK_LINES:]
                if len(use) < FRONT_BACK_LINES:
                    raise SystemExit(
                        f"{md.name} 折行后仅 {len(use)} 行，不足后30页材料（1500 行）"
                    )
                label = "后30页"
            else:
                use, label = folded, "全部"
            template = tpl or front_tpl or back_tpl or all_tpl
            if template is None:
                raise SystemExit(f"{set_dir.name}: 找不到任何代码 DOCX 作为模板")
            docx_out = workdir / "in" / f"{base_name}-源程序({label}).docx"
            docx_out.parent.mkdir(parents=True, exist_ok=True)
            expected = build_code_docx(template, use, docx_out,
                                       ascii_font=args.font_ascii, ea_font=args.font_ea)
            jobs.append(docx_out)
            job_meta[docx_out.stem] = {"expected_pages": expected, "kind": "code", "label": label}
            report["checks"].append(f"源程序({label}) 重分页后预期 {expected} 页")

        if front_tpl or front_md.exists():
            if front_md.exists():
                prepare_code(front_md, front_tpl, "front")
            else:
                jobs.append(front_tpl)
                job_meta[front_tpl.stem] = {"expected_pages": FRONT_BACK_PAGES, "kind": "code-docx", "label": "前30页"}
        if back_tpl or back_md.exists():
            if back_md.exists():
                prepare_code(back_md, back_tpl, "back")
            else:
                jobs.append(back_tpl)
                job_meta[back_tpl.stem] = {"expected_pages": FRONT_BACK_PAGES, "kind": "code-docx", "label": "后30页"}
        if not (front_md.exists() or back_md.exists()) and all_md.exists():
            prepare_code(all_md, all_tpl, "all")

        # ---- 文档鉴别材料（操作手册整本转换后切页） ----
        manual = find_one(preview, "*_操作手册.docx") or find_one(preview, "*操作手册*.docx")
        if manual is None:
            report["status"] = "partial"
            report["checks"].append("警告: 未找到操作手册 DOCX")
        else:
            jobs.append(manual)
            job_meta[manual.stem] = {"kind": "manual"}

        if not jobs:
            return {"set": set_dir.name, "status": "skipped", "reason": "没有可处理的材料"}

        pdfs = docker_convert(args.image, jobs, Path(args.host_fonts), workdir, args.image_tar)

        pdf_dir = preview / "PDF"
        pdf_dir.mkdir(exist_ok=True)
        backup_dir = pdf_dir / "分页备查"
        code_parts: dict[str, Path] = {}
        manual_full: Path | None = None

        for pdf in pdfs:
            meta = job_meta.get(pdf.stem, {})
            pages = pdf_page_count(pdf)
            kind = meta.get("kind")
            if kind == "code":
                expected = meta["expected_pages"]
                if pages != expected:
                    raise SystemExit(
                        f"{set_dir.name}: 源程序({meta['label']}) 实际 {pages} 页 != 预期 {expected} 页"
                    )
                if meta["label"] == "全部":
                    # 全部模式：整份即主材料，先留在工作目录，稍后由“全部”分支落到主目录
                    code_parts[meta["label"]] = pdf
                else:
                    backup_dir.mkdir(exist_ok=True)
                    dst = backup_dir / pdf.name
                    shutil.copy2(pdf, dst)
                    code_parts[meta["label"]] = dst
                    report["outputs"].append(f"分页备查/{dst.name}")
                report["checks"].append(f"源程序({meta['label']}) = {pages} 页 ✓")
            elif kind == "code-docx":
                if pages != meta["expected_pages"]:
                    raise SystemExit(f"{set_dir.name}: {pdf.stem} 实际 {pages} 页 != 预期 {meta['expected_pages']} 页")
                backup_dir.mkdir(exist_ok=True)
                dst = backup_dir / pdf.name.replace("-代码(", "-源程序(")
                shutil.copy2(pdf, dst)
                code_parts[meta["label"]] = dst
                report["outputs"].append(f"分页备查/{dst.name}")
                report["checks"].append(f"源程序({meta['label']}) = {pages} 页 ✓（直转）")
            else:
                manual_full = pdf
                report["checks"].append(f"操作手册整本 = {pages} 页")

        # 文档鉴别材料切页（主文件始终是一份合并 PDF）
        if manual_full is not None:
            pages = pdf_page_count(manual_full)
            base = manual_full.stem  # <软件全称>_操作手册
            soft_name = base.replace("_操作手册", "").replace("操作手册", "")
            if pages > FULL_SUBMIT_PAGES:
                backup_dir.mkdir(exist_ok=True)
                front_dst = backup_dir / f"{soft_name}-文档(前30页).pdf"
                back_dst = backup_dir / f"{soft_name}-文档(后30页).pdf"
                extract_pages(manual_full, 1, FRONT_BACK_PAGES, front_dst)
                extract_pages(manual_full, pages - FRONT_BACK_PAGES + 1, pages, back_dst)
                combined = pdf_dir / f"{soft_name}-文档鉴别材料(前30页+后30页合并).pdf"
                subprocess.run(["pdfunite", str(front_dst), str(back_dst), str(combined)], check=True)
                report["outputs"] += [combined.name, f"分页备查/{front_dst.name}", f"分页备查/{back_dst.name}"]
                report["checks"].append(f"文档 前30+后30 已从整本 {pages} 页切出并合并为一份")
            else:
                whole = pdf_dir / f"{soft_name}-文档鉴别材料(全部).pdf"
                shutil.copy2(manual_full, whole)
                report["outputs"].append(whole.name)
                report["checks"].append(f"文档共 {pages} 页 ≤60，整本一份提交")

        # 源程序 前30+后30 合并版（提交主文件）
        if "前30页" in code_parts and "后30页" in code_parts:
            soft = code_parts["前30页"].name.split("-源程序(")[0]
            combined = pdf_dir / f"{soft}-源程序鉴别材料(前30页+后30页合并).pdf"
            subprocess.run(
                ["pdfunite", str(code_parts["前30页"]), str(code_parts["后30页"]), str(combined)], check=True
            )
            report["outputs"].insert(0, combined.name)
        elif "全部" in code_parts:
            all_pdf = code_parts["全部"]
            n = pdf_page_count(all_pdf)
            soft = all_pdf.name.split("-源程序(")[0]
            if n > FULL_SUBMIT_PAGES:
                # 全部模式折行后超过 60 页：按规则切前30+后30 合并提交
                combined = pdf_dir / f"{soft}-源程序鉴别材料(前30页+后30页合并).pdf"
                front_tmp = workdir / "front30.pdf"
                back_tmp = workdir / "back30.pdf"
                extract_pages(all_pdf, 1, FRONT_BACK_PAGES, front_tmp)
                extract_pages(all_pdf, n - FRONT_BACK_PAGES + 1, n, back_tmp)
                subprocess.run(["pdfunite", str(front_tmp), str(back_tmp), str(combined)], check=True)
                report["outputs"].insert(0, combined.name)
                report["checks"].append(f"全部代码折行后 {n} 页 >60，已切前30+后30 合并为一份")
            else:
                dst = pdf_dir / all_pdf.name.replace("-源程序(全部)", "-源程序鉴别材料(全部)")
                shutil.copy2(all_pdf, dst)
                report["outputs"].insert(0, dst.name)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--materials-root", type=Path, help="软著资料根目录（批量处理其下所有子目录）")
    ap.add_argument("--set-dir", type=Path, help="单个软著目录")
    ap.add_argument("--image", default=DEFAULT_IMAGE, help=f"LibreOffice 容器镜像（默认 {DEFAULT_IMAGE}）")
    ap.add_argument("--image-tar", type=Path, default=Path.home() / "docker-images" / "softcopy-pdf.tar",
                    help="镜像被清理后自动 docker load 的持久化 tar")
    ap.add_argument("--host-fonts", default="/usr/share/fonts/opentype/noto", help="挂载给容器的 CJK 字体目录")
    ap.add_argument("--font-ascii", default=DEFAULT_FONT_ASCII, help="代码 ASCII 字体（容器内需存在）")
    ap.add_argument("--font-ea", default=DEFAULT_FONT_EA, help="代码 CJK 字体（容器内需存在）")
    args = ap.parse_args()

    if not args.materials_root and not args.set_dir:
        ap.error("需要 --materials-root 或 --set-dir")
    set_dirs = (
        sorted(p for p in args.materials_root.iterdir() if p.is_dir()) if args.materials_root else [args.set_dir]
    )

    ok = skip = fail = 0
    for sd in set_dirs:
        if sd.name.startswith("."):
            continue
        try:
            rep = process_set(sd, args)
        except SystemExit as e:
            fail += 1
            print(f"[FAIL] {sd.name}: {e}", flush=True)
            continue
        status = rep.get("status")
        if status == "skipped":
            skip += 1
            print(f"[SKIP] {sd.name}: {rep.get('reason')}", flush=True)
            continue
        ok += 1
        print(f"[OK] {sd.name}", flush=True)
        for c in rep.get("checks", []):
            print(f"      {c}", flush=True)
        out_dir = sd / "预览资料" if (sd / "预览资料").is_dir() else sd / "正式资料"
        lines = [f"# PDF 鉴别材料生成报告 — {sd.name}", ""]
        lines += [f"- {o}" for o in rep.get("outputs", [])]
        lines += ["", "## 校验", ""] + [f"- {c}" for c in rep.get("checks", [])]
        (out_dir / "PDF生成报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n完成: ok={ok} skip={skip} fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
