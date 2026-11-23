# -*- coding: utf-8 -*-
"""报告导出：Excel / Markdown"""
import os
import json

from paths import DATA_DIR
REPORT_DIR = os.path.join(DATA_DIR, "reports")


def _fmt_ts(sec):
    n = max(0, int(sec or 0))
    return f"{n // 60:02d}:{n % 60:02d}"


def to_markdown(report):
    st = report.get("stats") or {}
    an = report.get("analysis") or {}
    lines = [f"# {report.get('title', '视频分析报告')}", ""]
    lines.append("| 项目 | 信息 |")
    lines.append("|---|---|")
    lines.append(f"| 链接 | {report.get('url', '')} |")
    lines.append(f"| 时长 | {report.get('duration', 0):.0f} 秒 |")
    lines.append(f"| 语言 | {report.get('language', 'zh')} |")
    lines.append("")
    if st:
        badges = [f"{e} {st[k]}" for k, e in
                  [("like", "赞"), ("comment", "评"), ("collect", "藏"),
                   ("share", "转")] if str(st.get(k, "-")) != "-"]
        if badges:
            lines.append(" ".join(badges))
            lines.append("")
    if an.get("summary"):
        lines.append("## 总览")
        lines.append("")
        lines.append(f"- **一句话总结**: {an['summary']}")
        if an.get("scenes"):
            lines.append(f"- **适合场景**: {an['scenes']}")
        if an.get("structure"):
            lines.append(f"- **核心结构**: {an['structure']}")
        lines.append("")
    if an.get("highlights"):
        lines.append("## 值得学习")
        lines.append("")
        lines.append("| 值得学的点 | 原片怎么做的 | 仿写时怎么用 |")
        lines.append("|---|---|---|")
        for h in an["highlights"]:
            lines.append(f"| {h.get('point')} | {h.get('how')} | {h.get('use')} |")
        lines.append("")
    if an.get("improvements"):
        lines.append("## 改进建议")
        lines.append("")
        lines.append("| 调整点 | 原片的问题 | 怎么改 |")
        lines.append("|---|---|---|")
        for i in an["improvements"]:
            lines.append(f"| {i.get('point')} | {i.get('problem')} | {i.get('fix')} |")
        lines.append("")
    if report.get("original_segments"):
        lines.append("## 原内容（转写）")
        lines.append("")
        for s in report["original_segments"]:
            lines.append(f"- [{_fmt_ts(s.get('start'))}] {s.get('text', '')}")
        lines.append("")
    if report.get("rewritten_segments"):
        lines.append("## 润色改写")
        lines.append("")
        for s in report["rewritten_segments"]:
            lines.append(f"- [{_fmt_ts(s.get('start'))}] {s.get('rewritten', '')}")
        lines.append("")
    if report.get("vision"):
        lines.append("## 画面分析")
        lines.append("")
        for v in report["vision"]:
            lines.append(f"### {_fmt_ts(v.get('time'))}")
            lines.append("")
            lines.append(str(v.get("analysis", "")))
            lines.append("")
    return "\n".join(lines)


def to_xlsx(report, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()

    def head(ws, cell, text, bg="1F2329"):
        c = ws[cell]
        c.value = text
        c.font = Font(name="微软雅黑", size=12, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=bg)
        c.alignment = Alignment(horizontal="center", vertical="center")

    # Sheet1 总览
    ws = wb.active
    ws.title = "总览"
    head(ws, "A1", "📊 视频拆解报告")
    ws.merge_cells("A1:B1")
    ws.row_dimensions[1].height = 36
    st = report.get("stats") or {}
    an = report.get("analysis") or {}
    rows = [("视频ID", report.get("video_id")), ("标题", report.get("title")),
            ("链接", report.get("url")), ("时长(秒)", report.get("duration")),
            ("点赞", st.get("like")), ("评论", st.get("comment")),
            ("收藏", st.get("collect")), ("转发", st.get("share")),
            ("主播", st.get("nickname") or st.get("channel_name")),
            ("一句话总结", an.get("summary")), ("适合场景", an.get("scenes")),
            ("核心结构", an.get("structure"))]
    r = 3
    for k, v in rows:
        ws.cell(row=r, column=1, value=k).font = Font(name="微软雅黑", bold=True)
        ws.cell(row=r, column=2, value=str(v or "")).font = Font(name="微软雅黑", size=10)
        ws.cell(row=r, column=2).alignment = Alignment(wrap_text=True)
        r += 1
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 90

    # Sheet2 转写对照
    ws2 = wb.create_sheet("转写对照")
    head(ws2, "A1", "📄 原文 → 润色对照")
    ws2.merge_cells("A1:D1")
    for col, w in enumerate(["时间", "原文", "润色", "状态"], 1):
        c = ws2.cell(row=2, column=col, value=w)
        c.font = Font(name="微软雅黑", bold=True)
    orig = report.get("original_segments") or []
    r = 3
    for s in (report.get("rewritten_segments") or []):
        ws2.cell(row=r, column=1, value=_fmt_ts(s.get("start")))
        ws2.cell(row=r, column=2, value=s.get("original", ""))
        ws2.cell(row=r, column=3, value=s.get("rewritten", ""))
        r += 1
    if r == 3:
        for s in orig:
            ws2.cell(row=r, column=1, value=_fmt_ts(s.get("start")))
            ws2.cell(row=r, column=2, value=s.get("text", ""))
            r += 1
    ws2.column_dimensions["B"].width = 50
    ws2.column_dimensions["C"].width = 50

    # Sheet3 画面分析
    vision = report.get("vision") or []
    if vision:
        ws3 = wb.create_sheet("画面分析")
        head(ws3, "A1", "🎬 画面分析（整段视频理解）")
        ws3.merge_cells("A1:B1")
        r = 3
        for v in vision:
            c = ws3.cell(row=r, column=1, value=_fmt_ts(v.get("time")))
            c.font = Font(name="微软雅黑", bold=True, color="6C5CE7")
            c2 = ws3.cell(row=r, column=2, value=str(v.get("analysis", "")))
            c2.alignment = Alignment(wrap_text=True)
            r += 1
        ws3.column_dimensions["A"].width = 10
        ws3.column_dimensions["B"].width = 100

    wb.save(path)
    return path


def export(video_id, fmt):
    rfile = os.path.join(REPORT_DIR, f"{video_id}.json")
    if not os.path.exists(rfile):
        raise FileNotFoundError(f"报告不存在: {video_id}")
    report = json.load(open(rfile, encoding="utf-8"))
    out_dir = os.path.join(DATA_DIR, "exports")
    os.makedirs(out_dir, exist_ok=True)
    if fmt == "md":
        path = os.path.join(out_dir, f"{video_id}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(to_markdown(report))
    elif fmt == "xlsx":
        path = os.path.join(out_dir, f"{video_id}.xlsx")
        to_xlsx(report, path)
    else:
        raise ValueError(f"不支持的格式: {fmt}")
    return path
