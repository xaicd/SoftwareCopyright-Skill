# 鉴别材料 PDF 后端

## 职责边界

- `build_pdf_materials.py`（Python 标准库）负责：读取代码 Markdown、90 显示列折行、每 50 行硬分页的确定性重排、DOCX 模板复用、页数校验、PDF 切页与合并。
- Docker 镜像 `softcopy-pdf`（Debian stable-slim + LibreOffice Writer，`--no-install-recommends`）负责 DOCX→PDF 渲染；宿主机不安装 LibreOffice，也不引入其他渲染依赖。
- 页数确定性：代码每页 50 行 × 14pt 精确行距 = 700pt，小于 A4 版心可用高度约 728pt；页与页之间用段内 `<w:br w:type="page"/>` 显式分页。前/后 30 页材料各取 1500 个折行后的物理行，因此无论排版引擎如何，`pdfinfo` 页数恒为 30。
- 字体：代码 ASCII 使用容器内 `DejaVu Sans Mono`（90 列 × 4.21pt ≈ 379pt < 版心宽 467pt，不会横向换行）；中文使用只读挂载的宿主机 `Noto Serif CJK SC`（SimSun 的替换渲染，仅影响字形不影响分页约束）。

## 镜像构建（一次性）

Dockerfile 位于 `<SKILL_DIR>/docker/softcopy-pdf.Dockerfile`：

```bash
docker build -t softcopy-pdf <SKILL_DIR>/docker
```

（Dockerfile 内容：Debian stable-slim + 清华镜像源 + `libreoffice-writer fonts-dejavu-core fontconfig`，`--no-install-recommends`，入口 `soffice`。海外环境可删掉 `sed` 换源行。）

## 运行时约定

- **输出布局（登记系统按“一份材料一个 PDF”上传）**：每套 `预览资料/PDF/` 主目录只放两份合并单文件——`<软件全称>-源程序鉴别材料(前30页+后30页合并).pdf`（全部模式不足 60 页时为 `源程序鉴别材料(全部).pdf`）与 `<软件全称>-文档鉴别材料(全部).pdf`（超 60 页时为 `文档鉴别材料(前30页+后30页合并).pdf`）。前 30 页/后 30 页分开版放入 `分页备查/` 子目录，仅供核对，不用于提交。
- 每个软著目录的转换在独立临时目录进行，容器以 `--rm` 运行，逐文件 `soffice --headless --convert-to pdf`。
- 宿主机 `/usr/share/fonts/opentype/noto` 以只读挂载到容器同路径；若宿主机 CJK 字体目录不同，用 `--host-fonts` 覆盖。
- 转换后用 `pdfinfo` 校验页数；`gs -sPageList` 切文档前/后 30 页；`pdfunite` 合并前后 30 页版本；全部模式折行后超过 60 页的自动切前 30+后 30 合并。
- 不修改任何既有 DOCX；代码 PDF 的 DOCX 在临时目录重建（以现有代码 DOCX 为模板，仅替换 `word/document.xml` 正文段落，保留页眉页脚、节属性与样式）。
- **镜像持久化与自愈**：宿主机磁盘紧张时 docker 清理会误删本镜像。镜像 `docker save` 到 `~/docker-images/softcopy-pdf.tar`；脚本发现镜像缺失会自动 `docker load` 恢复（`--image-tar` 指定路径），无需网络。

## 常用命令

```bash
# 批量：对 软件著作权申请资料/ 下所有子目录生成 PDF 鉴别材料
python3 <SKILL_DIR>/scripts/build_pdf_materials.py --materials-root 软件著作权申请资料

# 单个软著目录
python3 <SKILL_DIR>/scripts/build_pdf_materials.py --set-dir 软件著作权申请资料/01-数字大集

# 页数复核
pdfinfo <软件全称>-源程序鉴别材料\(前30页+后30页合并\).pdf | grep Pages
```

## 已知边界

- 历史草稿若未做 90 列折行（早期版本生成），脚本会在 PDF 阶段统一重排，页数仍锁定 30 页；DOCX 源文件保持原样，不回写。
- **East Asian Width ambiguous 字符**（`─`、`→`、`≥`、`…` 等）在 CJK 字体下常渲染为全角。PDF 折行把它们按 2 列保守计算（`_col_width`），避免横向换行破坏每页 50 行约束（实际案例：30-消息通知 后30页曾因此溢出成 31 页）。`extract_code_material.py` 的 DOCX 路径仍按 W/F 2 列计，若源码大量使用此类字符，Word/WPS 下也可能出现同类漂移。
- 文档（操作手册）超过 60 页才切前/后 30 页；不足 60 页整本提交，输出 `文档(全部).pdf`。
- LibreOffice 与 Word 的排版差异只影响操作手册整本 PDF 的自然分页页数，不影响提交有效性；页码由页眉 PAGE 字段在 PDF 内自洽。
- 宿主机 docker daemon 若配置了 HTTP 代理而代理进程未运行，`docker pull` 会失败。临时处理：起本地透传代理（`/tmp` 下 pyproxy.py 示例）或改用可达镜像源（如 `docker.m.daocloud.io`，偶发 502 重试即可），完成后关闭。重建命令：`docker build -t softcopy-pdf <SKILL_DIR>/docker`。
- 手册 DOCX 由 `build_manual_docx.py`（python-docx）构建时依赖 `pip3 install --user python-docx`；设计章节由 `enrich_manual_design.py` 生成（详见 SKILL.md 第 9 步）。
