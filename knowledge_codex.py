"""Zero-LLM, field-weighted BM25F + Inverted Index knowledge codex for playrooms.

Extracts structured entries (spells, runes, potions, herbs, creatures, magic domains,
artifacts, locations, academics, factions, glossary terms, and public character profiles)
from playroom story_bible.json, source/worldbuilding/, source/glossary/, and story/.
"""
from __future__ import annotations

import copy
import math
import re
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

CODEX_CATEGORIES: List[Dict[str, str]] = [
    {"id": "spell", "label": "Thần chú", "icon": "✨", "hint": "Khẩu lệnh Galdr, bùa đời sống, chiến đấu & cấm thuật"},
    {"id": "rune", "label": "Cổ ngữ", "icon": "🪨", "hint": "Elder Futhark, nghĩa chính đạo, Merkstave & trận pháp"},
    {"id": "potion", "label": "Độc dược", "icon": "⚗️", "hint": "Công thức pha chế, thành phần, tác dụng & tai nạn nổ vạc"},
    {"id": "herb", "label": "Thảo dược", "icon": "🌿", "hint": "Thảo dược Ánh Trăng, dược tính, cách hái & nguyên liệu"},
    {"id": "creature", "label": "Sinh vật", "icon": "🦌", "hint": "Thú huyền bí, rồng, kỳ lân, linh vật nhà, chủng tộc & quái vật"},
    {"id": "magic", "label": "Ma thuật", "icon": "🌌", "hint": "Hệ thống ma thuật, Cửu Giới Thần Vực, dị năng & định luật"},
    {"id": "artifact", "label": "Vật phẩm & Cổ vật", "icon": "🏺", "hint": "Cổ vật, trang bị, đũa phép, cánh bay & tiền tệ"},
    {"id": "location", "label": "Địa điểm", "icon": "🏰", "hint": "Lâu đài, ký túc xá, đấu trường, rừng sâu & mật thất"},
    {"id": "academic", "label": "Môn học & Học chế", "icon": "🎓", "hint": "Môn học, giảng viên, lịch tuần, thang điểm & luật thi"},
    {"id": "faction", "label": "Gia tộc & Phe phái", "icon": "👑", "hint": "Các Nhà, cổ tộc, hội nhóm & hội đồng quản trị"},
    {"id": "glossary", "label": "Thuật ngữ", "icon": "📜", "hint": "Từ điển thuật ngữ, khái niệm & điều lệ thế giới"},
    {"id": "character", "label": "Nhân vật", "icon": "👤", "hint": "Hồ sơ công khai, vai trò, tính cách & sở trường"},
]

CATEGORY_ORDER = {cat["id"]: idx for idx, cat in enumerate(CODEX_CATEGORIES)}

NORSE_FOLD_MAP = str.maketrans({
    "đ": "d",
    "ð": "d",
    "þ": "th",
    "ß": "ss",
    "ø": "o",
    "ö": "o",
    "å": "a",
})


def fold_text(value: Any) -> str:
    """Fold Vietnamese + Old Norse diacritics into plain ASCII lowercase."""
    raw = str(value or "").casefold()
    raw = raw.replace("æ", "ae").replace("œ", "oe").translate(NORSE_FOLD_MAP)
    nfd = unicodedata.normalize("NFD", raw)
    return "".join(ch for ch in nfd if not unicodedata.combining(ch))


def tokenize(value: Any) -> List[str]:
    """Tokenize into canonical terms, also adding joined compound tokens for hyphenated spells."""
    folded = fold_text(value)
    words = re.findall(r"[a-z0-9]+", folded)
    tokens: List[str] = list(words)
    # Add joined form for hyphenated Norse commands/terms like Ljos-Kveikja -> ljoskveikja
    for compound in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", folded):
        joined = compound.replace("-", "")
        if joined and joined not in tokens:
            tokens.append(joined)
    return tokens


def clean_md(text: str) -> str:
    """Convert Markdown links and inline markers into clean readable prose."""
    if not text:
        return ""
    # Replace markdown links [Label](url) -> Label (strip .md suffix if link text equals filename)
    def _repl(match: re.Match) -> str:
        label = match.group(1).strip()
        if label.endswith(".md"):
            label = label[:-3].replace("-", " ").title()
        return label

    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _repl, text)
    out = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", out)
    out = re.sub(r"`([^`]+)`", r"\1", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def strip_frontmatter(raw: str) -> Tuple[Dict[str, Any], str]:
    """Parse simple YAML-like frontmatter without external yaml dependency."""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, raw
    meta: Dict[str, Any] = {}
    current_key: Optional[str] = None
    for line in lines[1:end_idx]:
        if re.match(r"^\s*-\s+", line) and current_key:
            item = re.sub(r"^\s*-\s+", "", line).strip().strip('"').strip("'")
            if isinstance(meta.get(current_key), list):
                meta[current_key].append(item)
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if m:
            k, v = m.group(1).strip(), m.group(2).strip().strip('"').strip("'")
            current_key = k
            if v == "":
                meta[k] = []
            else:
                meta[k] = v
    body = "\n".join(lines[end_idx + 1 :])
    return meta, body


def _parse_markdown_sections_and_tables(md_body: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extract hierarchical heading sections and markdown tables with their nearest section heading."""
    lines = md_body.splitlines()
    heading_stack: Dict[int, str] = {}
    tables: List[Dict[str, Any]] = []
    sections: List[Dict[str, Any]] = []
    current_section: Optional[Dict[str, Any]] = None
    i = 0

    def _current_heading_path() -> str:
        return " › ".join(heading_stack[lvl] for lvl in sorted(heading_stack) if heading_stack[lvl])

    while i < len(lines):
        line = lines[i]
        hm = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if hm:
            lvl = len(hm.group(1))
            title = clean_md(hm.group(2))
            heading_stack[lvl] = title
            for deeper in [k for k in list(heading_stack) if k > lvl]:
                del heading_stack[deeper]
            current_section = {"level": lvl, "title": title, "path": _current_heading_path(), "lines": []}
            sections.append(current_section)
            i += 1
            continue

        # Detect Markdown table: header row |...| followed by separator |---|
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:\-\|]+\|\s*$", lines[i + 1]):
            headers = [clean_md(cell) for cell in line.strip().strip("|").split("|")]
            rows: List[Dict[str, str]] = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [clean_md(cell) for cell in lines[j].strip().strip("|").split("|")]
                while len(cells) < len(headers):
                    cells.append("")
                row = {headers[col_idx]: cells[col_idx] for col_idx in range(len(headers)) if headers[col_idx]}
                rows.append(row)
                j += 1
            tables.append({
                "section_title": heading_stack.get(max(heading_stack, default=1), ""),
                "section_path": _current_heading_path(),
                "headers": headers,
                "rows": rows,
            })
            i = j
            continue

        if current_section is not None:
            current_section["lines"].append(line)
        i += 1

    return sections, tables


def _split_command_and_pronunciation(raw_cell: str) -> Tuple[str, str]:
    """Extract 'Ljós-Kveikja!' and '(Lió-sơ Kơ-vây-kia)' cleanly."""
    cleaned = clean_md(raw_cell)
    m = re.match(r"^(.+?)\s*\(([^()]+)\)\s*$", cleaned)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return cleaned, ""


def _make_entry(
    entry_id: str,
    category: str,
    title: str,
    *,
    subtitle: str = "",
    summary: str = "",
    details: Optional[Dict[str, str]] = None,
    tags: Optional[List[str]] = None,
    aliases: Optional[List[str]] = None,
    symbol: str = "",
    source: str = "",
    insert_text: str = "",
) -> Dict[str, Any]:
    clean_details = {k: str(v).strip() for k, v in (details or {}).items() if str(v or "").strip()}
    clean_tags = [str(t).strip() for t in (tags or []) if str(t or "").strip()]
    clean_aliases = [str(a).strip() for a in (aliases or []) if str(a or "").strip()]
    return {
        "id": entry_id,
        "category": category,
        "title": title.strip(),
        "subtitle": subtitle.strip(),
        "symbol": symbol.strip(),
        "summary": summary.strip(),
        "details": clean_details,
        "tags": clean_tags,
        "aliases": clean_aliases,
        "source": source.strip(),
        "insert_text": (insert_text or title).strip(),
    }


def _extract_grimoire_spells(md_text: str, source_name: str) -> List[Dict[str, Any]]:
    _, body = strip_frontmatter(md_text)
    sections, tables = _parse_markdown_sections_and_tables(body)
    entries: List[Dict[str, Any]] = []
    idx = 0

    for tbl in tables:
        headers = tbl["headers"]
        sec = tbl["section_title"]
        is_combat = "Loại " in sec or "Tấn Công" in tbl["section_path"]
        is_life = bool(re.match(r"^[A-Z]\.\s", sec))
        group_tag = "Cơ bản · Năm 1–5"
        if is_life:
            group_tag = f"Đời sống · {re.sub(r'^[A-Z]\.\s*', '', sec)}"
        elif is_combat:
            group_tag = f"Chiến đấu · {re.sub(r'^Loại\s*\d+\s*·\s*', '', sec)}"

        for row in tbl["rows"]:
            idx += 1
            raw_cmd = row.get(headers[0], "")
            cmd, pron = _split_command_and_pronunciation(raw_cmd)
            branch_or_level = ""
            gesture = ""
            effect = ""
            backlash = ""
            for h, val in row.items():
                hl = fold_text(h)
                if h == headers[0]:
                    continue
                if "nhanh" in hl or "cap do" in hl or "phan cap" in hl:
                    branch_or_level = val
                elif "cu chi" in hl or "dua phep" in hl:
                    gesture = val
                elif "tac dung" in hl or "cong dung" in hl:
                    effect = val
                elif "lech nhip" in hl or "tham hoa" in hl or "phan phe" in hl or "cai gia" in hl or "backlash" in hl:
                    backlash = val

            tags = ["Thần chú", "Galdr"]
            if branch_or_level:
                tags.append(branch_or_level)
            if is_combat:
                tags.append("Chiến đấu")
            elif is_life:
                tags.append("Đời sống")
            else:
                tags.append("Nền tảng")
            if "cam" in fold_text(branch_or_level):
                tags.append("Cấm thuật")

            subtitle = " · ".join(part for part in [f"Phát âm: {pron}" if pron else "", branch_or_level or group_tag] if part)
            entries.append(_make_entry(
                f"spell_{idx}_{fold_text(cmd).replace(' ', '_')[:32]}",
                "spell",
                cmd,
                subtitle=subtitle,
                summary=effect,
                details={
                    "Nhóm / Phân cấp": f"{group_tag}" + (f" ({branch_or_level})" if branch_or_level else ""),
                    "Cách đọc (Galdr)": pron,
                    "Cử chỉ Đũa phép": gesture,
                    "Công dụng": effect,
                    "Lệch nhịp / Phản phệ": backlash,
                },
                tags=tags,
                aliases=[pron, fold_text(cmd)] if pron else [fold_text(cmd)],
                source=source_name,
                insert_text=cmd,
            ))

    # Section 3: Special / Innate / Forbidden bloodline spells
    for sec in sections:
        if "3. Phép Thuật Đặc Thù" in sec["title"] or "Đặc Thù & Cấm Thuật" in sec["title"]:
            text_block = "\n".join(sec["lines"])
            for m in re.finditer(r"^\-\s+\*\*(.+?)\*\*\s*\n((?:\s+\-\s+.*\n?)+)", text_block, re.MULTILINE):
                raw_name = clean_md(m.group(1))
                name, note = _split_command_and_pronunciation(raw_name)
                bullets = [clean_md(re.sub(r"^\s*\-\s*", "", b)) for b in m.group(2).splitlines() if b.strip()]
                entries.append(_make_entry(
                    f"magic_innate_{fold_text(name).replace(' ', '_')[:32]}",
                    "magic",
                    name,
                    subtitle=note or "Dị năng & Cấm thuật đặc thù",
                    summary=" ".join(bullets),
                    details={"Mô tả": "\n".join(bullets)},
                    tags=["Dị năng", "Huyết mạch", "Cấm thuật"],
                    aliases=[note] if note else [],
                    source=source_name,
                    insert_text=name,
                ))
    return entries


def _extract_futhark_runes(md_text: str, source_name: str) -> List[Dict[str, Any]]:
    _, body = strip_frontmatter(md_text)
    sections, tables = _parse_markdown_sections_and_tables(body)
    entries: List[Dict[str, Any]] = []
    for tbl in tables:
        for idx, row in enumerate(tbl["rows"], 1):
            sym = row.get("Ký tự", "").strip()
            name = row.get("Tên Cổ Ngữ", "").strip()
            meaning = row.get("Ý nghĩa Nguyên bản", "").strip()
            upright = ""
            merk = ""
            for h, v in row.items():
                hf = fold_text(h)
                if "chinh dao" in hf or "upright" in hf:
                    upright = v
                elif "nghich dao" in hf or "merkstave" in hf:
                    merk = v
            if not name:
                continue
            entries.append(_make_entry(
                f"rune_{idx}_{fold_text(name)}",
                "rune",
                name,
                symbol=sym,
                subtitle=f"Ý nghĩa: {meaning}",
                summary=upright,
                details={
                    "Ký tự Elder Futhark": sym,
                    "Ý nghĩa nguyên bản": meaning,
                    "Chính đạo (Upright)": upright,
                    "Nghịch đảo (Merkstave)": merk,
                },
                tags=["Cổ ngữ", "Futhark", "Elder Futhark", "Merkstave" if "khong co" not in fold_text(merk) else "Đối xứng"],
                aliases=[sym, f"{sym} {name}", meaning],
                source=source_name,
                insert_text=f"{name} ({sym})" if sym else name,
            ))

    for sec in sections:
        if "Merkstave" in sec["title"]:
            txt = clean_md(" ".join(l for l in sec["lines"] if l.strip()))
            if txt:
                entries.append(_make_entry(
                    "rune_principle_merkstave",
                    "rune",
                    "Nguyên lý Cổ ngữ Đảo ngược (Merkstave)",
                    symbol="ᛉ↺",
                    subtitle="Quy luật trận pháp & phá hoại ngầm",
                    summary=txt,
                    details={"Cơ chế": txt},
                    tags=["Cổ ngữ", "Merkstave", "Trận pháp"],
                    source=source_name,
                    insert_text="Merkstave",
                ))
    return entries


def _extract_potions(md_text: str, source_name: str) -> List[Dict[str, Any]]:
    _, body = strip_frontmatter(md_text)
    sections, tables = _parse_markdown_sections_and_tables(body)
    entries: List[Dict[str, Any]] = []
    for tbl in tables:
        for idx, row in enumerate(tbl["rows"], 1):
            raw_name = row.get("Tên Độc Dược", "")
            name, latin = _split_command_and_pronunciation(raw_name)
            level = row.get("Cấp độ", "")
            ingredients = row.get("Thành phần chính", "")
            effect = row.get("Công dụng Chuẩn", "")
            fail = ""
            for h, v in row.items():
                if "pha sai" in fold_text(h):
                    fail = v
            if not name:
                continue
            entries.append(_make_entry(
                f"potion_{idx}_{fold_text(name)[:28].replace(' ', '_')}",
                "potion",
                name,
                subtitle=" · ".join(p for p in [latin, level] if p),
                summary=effect,
                details={
                    "Tên cổ / Hệ": latin,
                    "Cấp độ": level,
                    "Thành phần chính": ingredients,
                    "Công dụng chuẩn": effect,
                    "Phản ứng khi pha sai": fail,
                },
                tags=["Độc dược", "Giả kim", level],
                aliases=[latin] if latin else [],
                source=source_name,
                insert_text=name,
            ))

    for sec in sections:
        if "Tam Nguyên Tắc" in sec["title"] or "Tai Nạn Nổ Vạc" in sec["title"]:
            lines = [clean_md(re.sub(r"^\s*[\-\d\.]+\s*", "", l)) for l in sec["lines"] if l.strip()]
            if lines:
                entries.append(_make_entry(
                    f"potion_rule_{fold_text(sec['title'])[:24].replace(' ', '_')}",
                    "potion",
                    sec["title"],
                    subtitle="Quy tắc & Sự cố Hầm Độc Dược",
                    summary=" | ".join(lines),
                    details={"Chi tiết": "\n".join(lines)},
                    tags=["Độc dược", "Hầm Độc Dược", "Lửa Sương Hàn", "Lửa Hổ Phách"],
                    source=source_name,
                ))
    return entries


def _extract_creatures(md_text: str, source_name: str) -> List[Dict[str, Any]]:
    _, body = strip_frontmatter(md_text)
    sections, tables = _parse_markdown_sections_and_tables(body)
    entries: List[Dict[str, Any]] = []

    # Table-based Bestiary
    for tbl in tables:
        headers = tbl["headers"]
        for idx, row in enumerate(tbl["rows"], 1):
            raw_name = row.get(headers[0], "")
            name, alt = _split_command_and_pronunciation(raw_name)
            if not name:
                continue
            group = row.get("Phân Nhóm", "") or row.get("Nhà", "")
            appearance = row.get("Đặc điểm Ngoại hình & Tập tính", "") or row.get("Hình Thái & Biểu Tượng", "")
            magic_trait = row.get("Khả năng Phép thuật / Rắc rối Học đường", "") or row.get("Tập Tính & Tính Cách", "")
            potion_val = row.get("Giá trị trong Độc dược & Cổ vật", "") or row.get("Năng Lực Bảo Hộ & Vai Trò Trong Truyện", "")
            if headers[0] == "Nhà":
                # Four Living House Guardians table
                house = name
                raw_guardian = row.get("Tên Linh Vật Cổ", "")
                g_name, g_sub = _split_command_and_pronunciation(raw_guardian)
                name = g_name or raw_guardian
                alt = g_sub
                group = f"Linh vật Bảo hộ Nhà {house}"
            entries.append(_make_entry(
                f"creature_tbl_{idx}_{fold_text(name)[:28].replace(' ', '_')}",
                "creature",
                name,
                subtitle=" · ".join(p for p in [alt, group] if p),
                summary=magic_trait or appearance,
                details={
                    "Phân loại / Nhóm": group,
                    "Ngoại hình & Tập tính": appearance,
                    "Khả năng & Vai trò": magic_trait,
                    "Giá trị Độc dược / Bảo hộ": potion_val,
                },
                tags=["Sinh vật", group],
                aliases=[alt] if alt else [],
                source=source_name,
                insert_text=name,
            ))

    # Structured subheadings (Dragons, Unicorns, Phoenix, Abyssal Monsters, 6 Sapient Races)
    for sec in sections:
        t = sec["title"]
        if re.match(r"^(?:[A-D]\.|[1-6]\.)\s+", t) and ("Nhất" not in t and "Quản lý" not in t and "Tổng quan" not in t):
            # Ignore generic headers inside rules/curriculum files
            if not any(k in source_name for k in ("dragons", "abyssal", "races")):
                continue
            name_clean, alt = _split_command_and_pronunciation(re.sub(r"^(?:[A-D]|[1-6])\.\s*", "", t))
            details: Dict[str, str] = {}
            cur_key = "Tổng quan"
            buf: List[str] = []
            for raw_line in sec["lines"]:
                line = raw_line.strip()
                if not line or line == "---":
                    continue
                km = re.match(r"^\-\s+\*\*([^*:]+):?\*\*\s*:?\s*(.*)$", line)
                if km:
                    if buf:
                        details[cur_key] = clean_md(" ".join(buf))
                        buf = []
                    cur_key = clean_md(km.group(1)).rstrip(":")
                    rest = clean_md(km.group(2))
                    if rest:
                        buf.append(rest)
                else:
                    buf.append(clean_md(re.sub(r"^\s*[\-\d\.]+\s*", "", line)))
            if buf:
                details[cur_key] = clean_md(" ".join(buf))
            summary = details.get("Bản chất & Ngoại hình") or details.get("Ngoại hình") or details.get("Ngoại hình & Sinh học") or details.get("Thế mạnh") or next(iter(details.values()), "")
            tags = ["Sinh vật"]
            if "abyssal" in source_name:
                tags.append("Quái vật Hắc ám")
            elif "dragons" in source_name:
                tags.append("Thần thú Thượng cổ")
            elif "races" in source_name:
                tags.append("Chủng tộc Dị nhân")
            entries.append(_make_entry(
                f"creature_sec_{fold_text(name_clean)[:32].replace(' ', '_')}",
                "creature",
                name_clean,
                subtitle=alt,
                summary=summary,
                details=details,
                tags=tags,
                aliases=[alt] if alt else [],
                source=source_name,
                insert_text=name_clean,
            ))

    # Bestiary Nhóm I-IV overview
    if "bestiary" in source_name:
        for sec in sections:
            if "Phân cấp Quản lý Sinh vật" in sec["title"]:
                lines = [clean_md(re.sub(r"^\-\s*", "", l)) for l in sec["lines"] if l.strip()]
                if lines:
                    entries.append(_make_entry(
                        "creature_classification_groups",
                        "creature",
                        "Phân cấp 4 Nhóm Quản lý Sinh vật (Nhóm I–IV)",
                        subtitle="Quy định nuôi & tiếp xúc tại Hexenwyck",
                        summary=" | ".join(lines),
                        details={"Quy định 4 nhóm": "\n".join(lines)},
                        tags=["Sinh vật", "Học chế", "Nhóm I", "Nhóm II", "Nhóm III", "Nhóm IV"],
                        source=source_name,
                    ))
    return entries


def _extract_nine_realms(md_text: str, source_name: str) -> List[Dict[str, Any]]:
    _, body = strip_frontmatter(md_text)
    sections, tables = _parse_markdown_sections_and_tables(body)
    entries: List[Dict[str, Any]] = []

    # Map tables by section title
    table_by_sec = {tbl["section_title"]: tbl for tbl in tables}

    for sec in sections:
        t = sec["title"]
        if "Quy Luật Cốt Lõi" in t:
            rules = [clean_md(re.sub(r"^\s*[\-\d\.]+\s*", "", l)) for l in sec["lines"] if l.strip()]
            entries.append(_make_entry(
                "magic_yggdrasil_core_rules",
                "magic",
                "Cửu Giới Thần Vực — Quy Luật Cốt Lõi (Yggdrasil Domain)",
                subtitle="Ma thuật Lãnh địa tối thượng của Lucian Evander",
                summary=" ".join(rules[:4]),
                details={"Quy luật & Giới hạn": "\n".join(rules)},
                tags=["Ma thuật", "Lãnh địa", "Yggdrasil", "Cửu Giới Thần Vực", "Hình phạt Norns"],
                source=source_name,
                insert_text="Cửu Giới Thần Vực",
            ))
            continue

        rm = re.match(r"^\d+\.\s+(.+?)\s*\((.+)\)$", t)
        if rm:
            realm_name = rm.group(1).strip()
            realm_role = rm.group(2).strip()
            details: Dict[str, str] = {}
            summon_title = ""
            weakness = ""
            context_desc = ""
            passive = ""
            lines_clean: List[str] = []
            for raw in sec["lines"]:
                s = raw.strip()
                if not s or s == "---":
                    continue
                cl = clean_md(s)
                if "Bối cảnh không gian:" in cl:
                    context_desc = cl.split("Bối cảnh không gian:", 1)[1].strip()
                elif "Hiệu ứng đặc trưng:" in cl:
                    passive = cl.split("Hiệu ứng đặc trưng:", 1)[1].strip()
                elif "BÍ THUẬT TRIỆU HỒI:" in cl:
                    summon_title = cl.split("BÍ THUẬT TRIỆU HỒI:", 1)[1].strip()
                    lines_clean.append(f"Triệu hồi: {summon_title}")
                elif "NHƯỢC ĐIỂM CHIẾN THUẬT:" in cl:
                    weakness = cl.split("NHƯỢC ĐIỂM CHIẾN THUẬT:", 1)[1].strip()
                else:
                    lines_clean.append(re.sub(r"^\s*\-\s*", "", cl))

            tbl = table_by_sec.get(t)
            skills_text = ""
            if tbl and tbl["rows"]:
                skills_text = "\n".join(
                    f"• {r.get('Tên Kỹ Năng / Chiêu Thức', '')}: {r.get('Mô Tả Hiệu Ứng Chi Tiết', '')}"
                    for r in tbl["rows"]
                )
            details["Bối cảnh không gian"] = context_desc
            details["Hiệu ứng Lãnh địa"] = passive
            if skills_text:
                details["Chiêu thức & Tất sát"] = skills_text
            if summon_title:
                details["Bí thuật Triệu hồi"] = summon_title
            if lines_clean:
                details["Chi tiết bổ sung"] = "\n".join(lines_clean[:8])
            details["Nhược điểm chiến thuật"] = weakness

            entries.append(_make_entry(
                f"magic_realm_{fold_text(realm_name).replace(' ', '_')}",
                "magic",
                realm_name,
                subtitle=realm_role,
                summary=passive or context_desc,
                details=details,
                tags=["Ma thuật", "Cửu Giới Thần Vực", "Lãnh địa", realm_name.split()[0]],
                aliases=[realm_name.split()[0], realm_role],
                source=source_name,
                insert_text=realm_name,
            ))
    return entries


def _extract_standard_markdown_entry(md_text: str, category: str, file_path: Path, source_name: str) -> List[Dict[str, Any]]:
    meta, body = strip_frontmatter(md_text)
    sections, _ = _parse_markdown_sections_and_tables(body)
    title = str(meta.get("name") or meta.get("term") or file_path.stem.replace("-", " ").title()).strip()
    meta_type = str(meta.get("type") or meta.get("category") or "").strip()

    details: Dict[str, str] = {}
    summary = ""
    for sec in sections:
        if sec["level"] == 1:
            continue
        content_lines = [clean_md(re.sub(r"^\s*[\-\d\.]+\s*", "", l)) for l in sec["lines"] if l.strip() and l.strip() != "---"]
        joined = "\n".join(content_lines)
        if not joined:
            continue
        if not summary and sec["title"].lower() in {"overview", "description", "definition", "purpose"}:
            summary = content_lines[0] if len(content_lines[0]) <= 340 else content_lines[0][:337] + "…"
        details[sec["title"]] = joined

    if not summary and details:
        first_val = next(iter(details.values()), "")
        summary = first_val.splitlines()[0][:320]

    tags = [t for t in (meta.get("tags") or []) if isinstance(t, str)]
    if meta_type:
        tags.append(meta_type)
    aliases = [a for a in (meta.get("aliases") or []) if isinstance(a, str)]

    return [_make_entry(
        f"{category}_{file_path.stem}",
        category,
        title,
        subtitle=meta_type.title() if meta_type else "",
        summary=summary,
        details=details,
        tags=tags,
        aliases=aliases,
        source=source_name,
        insert_text=title,
    )]


def _extract_hexenwyck_world_extras(story_bible: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract academic codex subjects, herbs, artifacts, currency, and rules from story_bible.json."""
    entries: List[Dict[str, Any]] = []
    world = story_bible.get("world") or {}
    codex = world.get("academic_codex") or {}
    nine_subjects = codex.get("nine_subjects") or {}

    herb_catalog = [
        ("Bạc Hà Sương Giá", "Thảo dược giải độc · Khắc tinh Kraken-Spawn", "Thành phần chính của Dịch Giải Độc Vạn Năng Bạc Hà (Eir's Mint Antidote) và Thuốc Trấn Tâm Người Sói; tinh dầu đậm đặc làm co rút xúc tu Kraken-Spawn tức khắc."),
        ("Quả Huyết Mọng Bắc Cực", "Nguyên liệu Huyết dịch cổ tộc", "Thành phần thiết yếu điều chế Huyết Dịch Thay Thế Cổ Tộc (Crimson-Vein Tonic) dành cho học sinh Huyết tộc như Jasper Ravenscroft."),
        ("Rễ Valerian Đen", "Thảo dược an thần · Gây ngủ sâu", "Dùng trong Thuốc Ngủ Không Mộng Mị (Draugr-Slumber Draught); nếu quá nửa giọt tinh dầu sẽ biến thành thuốc gây mộng du."),
        ("Hoa Anh Túc Tuyết", "Thảo dược cực hàn vùng vịnh", "Kết hợp với Rễ Valerian Đen và giọt sương đông chí tạo nên Thuốc Ngủ Không Mộng Mị giúp hồi phục ma lực."),
        ("Cỏ Đom Đóm Rễ Đan", "Thảo dược phát quang & Sơ cứu", "Phát sáng dịu tự nhiên như đèn sinh học và có dược tính cầm máu, làm lành vết thương nhẹ."),
        ("Nhựa Tần Bì Ấm", "Nhựa cây linh mộc · Trị Sương Hàn", "Thành phần chính của Cao Dán Tan Sương Hàn (Rimeburn Salve) và khi kết hợp cổ ngữ lửa Kaunaz kép có thể làm cứng khớp cánh Rồng Bão Tuyết."),
        ("Mandrake Bắc Cực", "Thảo dược nguy hiểm · Có âm thanh", "Phát ra tiếng hét âm băng gây choáng khi nhổ khỏi đất; bắt buộc phải đeo bịt tai lông nhím khi chăm sóc hoặc phân loại tại Nhà Kính / Tháp Kỷ Luật."),
        ("Rêu Phát Sáng Đảo Ngược", "Nguyên liệu Ảo ảnh & Tàng hình", "Kết hợp hơi thở Nhím Sương Mù và nước băng tan để điều chế Thuốc Tàng Hình Sương Mù (Mist-Weave Draught) kéo dài 15 phút."),
        ("Hoa Sen Cực Quang", "Thảo dược thượng hạng (Hiếm)", "Thành phần cốt lõi của Dược Phẩm Trấn Định Mạch Wyrd (Wyrd-Anchor Elixir) giúp Finnick Wren kìm hãm mạch cổ thuật bùng nổ ở Arc 3."),
        ("Độc Phụ Tử Bắc Cực (Nordic Wolfsbane)", "Kịch độc khắc chế Người Sói", "Độc thảo dược gây tê liệt hệ thần kinh tộc Người Sói Ulfhednar nhanh gấp 3 lần người thường; ngược lại liều vi lượng dùng nấu Thuốc Trấn Tâm Bạc Hà & Phụ Tử."),
        ("Quy tắc hái Thảo dược Ánh Trăng", "Nguyên tắc thu hoạch · Nhà Kính", "Phải hái đúng chu kỳ trăng bằng dao đồng hoặc dao bạc; tuyệt đối không dùng dao sắt thường vì làm hỏng hoàn toàn dược tính."),
    ]
    if nine_subjects.get("SUBJ-06_thaoduoc_anhtrang"):
        for idx, (h_name, h_sub, h_desc) in enumerate(herb_catalog, 1):
            entries.append(_make_entry(
                f"herb_{idx}_{fold_text(h_name)[:28].replace(' ', '_')}",
                "herb",
                h_name,
                subtitle=h_sub,
                summary=h_desc,
                details={
                    "Giáo trình": "Thảo dược học Ánh Trăng — Magistra Elowen Vane-Myrddin (Nhà Kính Cánh Tây)",
                    "Dược tính & Ứng dụng": h_desc,
                },
                tags=["Thảo dược", "Nhà Kính Ánh Trăng", "Độc dược"],
                source="story_bible.json · academic_codex",
                insert_text=h_name,
            ))

    # 9 Subjects + grading + schedule
    subject_meta_titles = {
        "SUBJ-01_buachu_galdr": ("Bùa chú & Âm luật Galdr (SUBJ-01)", "Magister Caedmon Ljóð · Giảng đường Vòm Âm Vang"),
        "SUBJ-02_docduoc_giakim": ("Độc dược & Giả kim Bắc Âu (SUBJ-02)", "Magister Severin Vespera · Hầm Độc Dược Đá Đen"),
        "SUBJ-03_bienhinh_merlin": ("Biến hình & Ảo ảnh Merlin (SUBJ-03)", "Magistra Sigrid Valkoinen · Sảnh Gương Biến Hình"),
        "SUBJ-04_congu_futhark": ("Cổ ngữ Futhark & Trận pháp (SUBJ-04)", "Magistra Astrid Stjernberg · Phòng Khắc Đá Cổ Ngữ"),
        "SUBJ-05_chiemtinh_seidr": ("Chiêm tinh & Tiên tri Seiðr (SUBJ-05)", "Magistra Astrid Stjernberg · Đài Vọng Tinh Tháp Bắc"),
        "SUBJ-06_thaoduoc_anhtrang": ("Thảo dược học Ánh Trăng (SUBJ-06)", "Magistra Elowen Vane-Myrddin · Nhà Kính Cánh Tây"),
        "SUBJ-07_sinhvat_huyenbi": ("Sinh vật Huyền bí (SUBJ-07)", "Magistra Elowen Vane-Myrddin · Trảng Chuồng Bìa Rừng"),
        "SUBJ-08_phongchong_hacam": ("Phòng chống Cổ thuật Hắc ám (SUBJ-08)", "Custos Torvald Skjoldr · Võ Đường Khiên Sắt"),
        "SUBJ-09_lichsu_mathuat": ("Lịch sử Ma thuật & Biên niên sử (SUBJ-09)", "Archivarius Haldor Mimirsson · Giảng đường Cổ Thư"),
    }
    for key, val in nine_subjects.items():
        if key in subject_meta_titles and isinstance(val, str):
            title, sub = subject_meta_titles[key]
            entries.append(_make_entry(
                f"academic_{key}",
                "academic",
                title,
                subtitle=sub,
                summary=val,
                details={"Nội dung chính khóa": val},
                tags=["Môn học", "Học chế Hexenwyck", sub.split("·")[0].strip()],
                source="story_bible.json · academic_codex",
                insert_text=title.split(" (")[0],
            ))

    if isinstance(nine_subjects.get("grading_and_exams"), str):
        entries.append(_make_entry(
            "academic_grading_exams",
            "academic",
            "Thang điểm 5 bậc Cổ ngữ (O / E / A / M / T) & Kỳ thi",
            subtitle="Hệ thống chấm điểm Học viện Hexenwyck",
            summary=nine_subjects["grading_and_exams"],
            details={"Quy chuẩn chấm điểm": nine_subjects["grading_and_exams"]},
            tags=["Học chế", "Thang điểm", "O", "E", "A", "M", "T"],
            source="story_bible.json · academic_codex",
        ))
    if isinstance(nine_subjects.get("weekly_schedule_year5"), str):
        entries.append(_make_entry(
            "academic_schedule_y5",
            "academic",
            "Thời khóa biểu Năm 5 & Nhịp sinh hoạt Hexenwyck",
            subtitle="Lịch học Thứ 2 – Thứ 7 · Chuông 07:00 · Giới nghiêm 22:00",
            summary=nine_subjects["weekly_schedule_year5"],
            details={"Lịch trình": nine_subjects["weekly_schedule_year5"]},
            tags=["Học chế", "Thời khóa biểu", "Năm 5", "Giới nghiêm"],
            source="story_bible.json · academic_codex",
        ))

    # Hexwings sport & currency & wands if Hexenwyck
    magic_sys = world.get("magic_system") or {}
    if isinstance(magic_sys.get("hexwings_sport"), str):
        entries.append(_make_entry(
            "academic_hexwings_rules",
            "academic",
            "Luật thi đấu Không chiến Hexwings",
            subtitle="7 tuyển thủ · Tinh Cầu Mặt Trời · Sắt Sấm · Chim Én Cực Quang",
            summary=magic_sys["hexwings_sport"],
            details={"Luật & Trang bị": magic_sys["hexwings_sport"]},
            tags=["Hexwings", "Thể thao", "Luật"],
            source="story_bible.json",
            insert_text="Hexwings",
        ))
        entries.append(_make_entry(
            "artifact_hexwings_gear",
            "artifact",
            "Trang bị Hexwings: Bộ Cánh Khắc Cổ Ngữ, Tinh Cầu, Sắt Sấm & Chim Én",
            subtitle="Pháp khí bay & bóng thi đấu tại Đấu trường Trên Không",
            summary=magic_sys["hexwings_sport"],
            details={
                "Đôi Cánh Khắc Cổ Ngữ (Hexwings Rig)": "Khắc cổ ngữ Raidho (Chuyển động) và Kaunaz (Lửa dẫn đường); tự bung tán dù ánh sáng nếu quá tải.",
                "Tinh Cầu Mặt Trời (Sun-Orb)": "Bóng ghi bàn của 3 Truy thủ, mỗi bàn 10 điểm.",
                "Sắt Sấm (Thunder-Bolts)": "2 quả cầu kim loại mang điện tích cổ ngữ lao vào người giữ bóng; Hộ vệ Gió dùng gậy đánh bật.",
                "Chim Én Cực Quang (Aurora-Swallow)": "Tạo vật cơ khí phép thuật tốc độ cao; Tầm Tinh Thủ bắt được sẽ kết thúc trận và mang về 50 điểm.",
            },
            tags=["Vật phẩm", "Hexwings", "Pháp khí", "Raidho", "Kaunaz"],
            source="story_bible.json",
            insert_text="Hexwings Rig",
        ))

    if world.get("realm") and "everwyck" in fold_text(world.get("realm", "")):
        entries.append(_make_entry(
            "artifact_runed_wand_currency",
            "artifact",
            "Đũa phép Khắc Cổ Ngữ & Tiền tệ Everwyck",
            subtitle="Vật dẫn phép (Focus) & Hệ thống Sól-mark / Máni-shilling / Eir-pence",
            summary="Đũa phép/Trượng làm từ gỗ linh mộc Bắc Âu (Tần bì Yggdrasil, Thủy tùng, Bạch dương bạc, Sồi đen) kết hợp lõi thú huyền bí. Tiền tệ gồm Sól-mark (vàng), Máni-shilling (bạc) và Eir-pence (đồng đỏ).",
            details={
                "Đũa phép (Runed Wand / Stave)": "Gỗ Tần bì Yggdrasil, Thủy tùng, Bạch dương bạc, Sồi đen; lõi lông Quạ Sương, vảy Rồng Vịnh Hẹp, gân Tuần Lộc Tinh Tú hoặc lông Kỳ Lân Cực Quang.",
                "Hệ thống tiền tệ": "1 Sól-mark (Vàng) = 12 Máni-shilling (Bạc); 1 Máni-shilling = 20 Eir-pence (Đồng lá đỏ).",
            },
            tags=["Vật phẩm", "Đũa phép", "Tiền tệ", "Sól-mark", "Máni-shilling", "Eir-pence"],
            source="worldbuilding/systems",
            insert_text="Đũa phép Khắc Cổ Ngữ",
        ))

    # Midnight Mischief Charter
    factions_bl = world.get("factions_and_bloodlines") or {}
    if isinstance(factions_bl.get("midnight_mischief_charter"), str):
        entries.append(_make_entry(
            "faction_midnight_mischief_charter",
            "faction",
            "Điều lệ Hội Nghịch Ngợm Đêm Khuya (8 Điều & Khẩu hiệu)",
            subtitle="Căn cứ Phòng Đồ Thất Lạc · Sau 22h không phân Nhà",
            summary=factions_bl["midnight_mischief_charter"][:280] + "…",
            details={"Toàn văn Điều lệ & Khẩu hiệu": factions_bl["midnight_mischief_charter"]},
            tags=["Phe phái", "Hội Nghịch Ngợm Đêm Khuya", "Điều lệ"],
            source="story_bible.json",
            insert_text="Hội Nghịch Ngợm Đêm Khuya",
        ))
    return entries


def _extract_generic_story_bible(story_bible: Dict[str, Any], room_state: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Extract general worldbuilding, locations, magic, rules, and public characters from any playroom's story_bible."""
    entries: List[Dict[str, Any]] = []
    world = story_bible.get("world") or {}

    if isinstance(world.get("magic"), dict):
        for k, v in world["magic"].items():
            if not v:
                continue
            label = k.replace("_", " ").title()
            entries.append(_make_entry(
                f"magic_bible_{k}",
                "magic",
                f"Ma thuật: {label}",
                subtitle="Hệ thống ma thuật trong canon",
                summary=str(v),
                details={"Quy tắc & Cơ chế": str(v)},
                tags=["Ma thuật", label],
                source="story_bible.json · world.magic",
                insert_text=label,
            ))

    if isinstance(world.get("locations"), dict):
        for k, v in world["locations"].items():
            if not v:
                continue
            label = k.replace("_", " ").title()
            entries.append(_make_entry(
                f"location_bible_{k}",
                "location",
                label,
                subtitle="Địa điểm chính trong bối cảnh",
                summary=str(v),
                details={"Mô tả": str(v)},
                tags=["Địa điểm", label],
                source="story_bible.json · world.locations",
                insert_text=label,
            ))

    if isinstance(world.get("guild_system"), dict):
        gs = world["guild_system"]
        for k, v in gs.items():
            txt = ", ".join(v) if isinstance(v, list) else str(v)
            entries.append(_make_entry(
                f"faction_guild_{k}",
                "faction",
                f"Hội & Tổ chức: {k.replace('_', ' ').title()}",
                subtitle="Hệ thống hội quán",
                summary=txt,
                details={"Thông tin": txt},
                tags=["Phe phái", "Hội"],
                source="story_bible.json · guild_system",
            ))

    # Extract public character summaries WITHOUT touching 'secrets' or hidden spoiler fields
    characters = story_bible.get("characters") or {}
    if isinstance(characters, dict) and characters:
        for cid, prof in characters.items():
            if not isinstance(prof, dict):
                continue
            name = prof.get("name") or cid
            role = prof.get("role") or ""
            public_face = prof.get("public_face") or ""
            personality = prof.get("personality") or ""
            voice = prof.get("voice") or ""
            age = prof.get("age")
            details = {}
            if age is not None:
                details["Tuổi"] = str(age)
            if role:
                details["Vai trò"] = str(role)
            if public_face:
                details["Diện mạo & Ấn tượng công khai"] = str(public_face)
            if personality:
                details["Tính cách"] = str(personality)
            if voice:
                details["Giọng điệu"] = str(voice)
            entries.append(_make_entry(
                f"char_{cid}",
                "character",
                str(name),
                subtitle=str(role)[:90],
                summary=str(public_face or personality or role),
                details=details,
                tags=["Nhân vật"],
                source="story_bible.json · characters",
                insert_text=str(name),
            ))
    elif room_state and isinstance(room_state.get("roster"), dict):
        for cid, npc in room_state["roster"].items():
            if not isinstance(npc, dict):
                continue
            name = npc.get("name") or cid
            role = npc.get("role") or ""
            pers = npc.get("personality") or ""
            entries.append(_make_entry(
                f"char_{cid}",
                "character",
                str(name),
                subtitle=str(role)[:90],
                summary=str(pers or role),
                details={"Vai trò": str(role), "Tính cách": str(pers)},
                tags=["Nhân vật"],
                source="room_state.json · roster",
                insert_text=str(name),
            ))
    return entries


def _compute_dir_fingerprint(playroom_dir: Path) -> Tuple[Tuple[str, int, int], ...]:
    items: List[Tuple[str, int, int]] = []
    for rel in ("story_bible.json",):
        p = playroom_dir / rel
        if p.is_file():
            st = p.stat()
            items.append((rel, st.st_mtime_ns, st.st_size))
    for folder in ("source/worldbuilding", "source/glossary", "story/worldbuilding", "story/glossary"):
        base = playroom_dir / folder
        if base.is_dir():
            for md in sorted(base.rglob("*.md")):
                st = md.stat()
                items.append((str(md.relative_to(playroom_dir)), st.st_mtime_ns, st.st_size))
    return tuple(items)


@lru_cache(maxsize=16)
def _build_cached_codex(playroom_dir_str: str, fingerprint: Tuple[Tuple[str, int, int], ...], bible_json_key: str) -> Tuple[Dict[str, Any], ...]:
    import json

    playroom_dir = Path(playroom_dir_str)
    story_bible = json.loads(bible_json_key) if bible_json_key else {}
    entries: List[Dict[str, Any]] = []
    seen_ids = set()

    def _add(batch: Sequence[Dict[str, Any]]) -> None:
        for item in batch:
            eid = item["id"]
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            entries.append(item)

    # 1. Structured worldbuilding/glossary markdown files (Hexenwyck / Storyrooms)
    for folder_root in ("source/worldbuilding", "story/worldbuilding"):
        wb_dir = playroom_dir / folder_root
        if not wb_dir.is_dir():
            continue
        for md_path in sorted(wb_dir.rglob("*.md")):
            if md_path.name.startswith("_"):
                continue
            rel = str(md_path.relative_to(playroom_dir)).replace("\\", "/")
            text = md_path.read_text(encoding="utf-8")
            name = md_path.name
            if name == "grimoire-of-spells-and-galdr.md":
                _add(_extract_grimoire_spells(text, rel))
            elif name == "futhark-runes-and-ward-weaving.md":
                _add(_extract_futhark_runes(text, rel))
            elif name == "nordic-potions-and-alchemy.md":
                _add(_extract_potions(text, rel))
            elif name in ("bestiary-of-everwyck.md", "dragons-unicorns-and-sacred-beasts.md", "abyssal-monsters-and-corrupted-beasts.md", "non-human-sapient-races.md"):
                _add(_extract_creatures(text, rel))
            elif name == "hexenwyck-faculty-and-house-spirits.md":
                # Guardians go to creatures; faculty/punishment rules go to academic
                _add(_extract_creatures(text, rel))
                _add(_extract_standard_markdown_entry(text, "academic", md_path, rel))
            elif name == "yggdrasil-nine-realms-domain-magic.md":
                _add(_extract_nine_realms(text, rel))
            elif name == "wyrd-and-threefold-magic.md":
                _add(_extract_standard_markdown_entry(text, "magic", md_path, rel))
            elif "artifacts" in md_path.parts:
                _add(_extract_standard_markdown_entry(text, "artifact", md_path, rel))
            elif "locations" in md_path.parts:
                _add(_extract_standard_markdown_entry(text, "location", md_path, rel))
            elif "factions" in md_path.parts:
                _add(_extract_standard_markdown_entry(text, "faction", md_path, rel))
            elif name in ("hexenwyck-curriculum-and-house-cup.md", "hexwings-aerial-sport.md"):
                _add(_extract_standard_markdown_entry(text, "academic", md_path, rel))
            elif name == "everwyck-conclave-and-bloodlines.md":
                _add(_extract_standard_markdown_entry(text, "faction", md_path, rel))
            else:
                _add(_extract_standard_markdown_entry(text, "glossary", md_path, rel))

    for gl_root in ("source/glossary", "story/glossary"):
        gl_dir = playroom_dir / gl_root
        if not gl_dir.is_dir():
            continue
        for md_path in sorted(gl_dir.rglob("*.md")):
            if md_path.name.startswith("_"):
                continue
            rel = str(md_path.relative_to(playroom_dir)).replace("\\", "/")
            text = md_path.read_text(encoding="utf-8")
            _add(_extract_standard_markdown_entry(text, "glossary", md_path, rel))

    # 2. Hexenwyck rich extras from story_bible.json (9 subjects, 10 herbs, currency, Hexwings gear, charter)
    _add(_extract_hexenwyck_world_extras(story_bible))

    # 3. Generic world/characters fallback for all playrooms (including Fairy Tail, Xuân Ý Kinh Hoa, etc.)
    _add(_extract_generic_story_bible(story_bible))

    # Sort canonically by category priority then insertion order
    entries.sort(key=lambda e: CATEGORY_ORDER.get(e["category"], 99))
    return tuple(entries)


class CodexSearchEngine:
    """Precomputed field-weighted BM25F + Inverted Index + Prefix/Bigram fallback engine."""

    FIELD_WEIGHTS = {
        "title": 5.0,
        "symbol": 4.5,
        "aliases": 4.0,
        "subtitle": 2.4,
        "tags": 2.2,
        "summary": 1.6,
        "details": 1.1,
    }

    def __init__(self, entries: Sequence[Dict[str, Any]]):
        self.entries = list(entries)
        self.n = len(self.entries)
        self.postings: Dict[str, List[Tuple[int, float]]] = {}
        self.idf: Dict[str, float] = {}
        self.doc_norms: List[float] = []
        self.folded_titles: List[str] = []
        self.folded_aliases: List[List[str]] = []
        self.folded_bodies: List[str] = []
        self._build_index()

    def _build_index(self) -> None:
        if not self.entries:
            return
        total_len = 0
        raw_docs: List[Dict[str, float]] = []
        for idx, entry in enumerate(self.entries):
            self.folded_titles.append(fold_text(entry.get("title", "")))
            self.folded_aliases.append([fold_text(a) for a in entry.get("aliases", [])])
            details_text = " ".join(f"{k} {v}" for k, v in (entry.get("details") or {}).items())
            self.folded_bodies.append(fold_text(f"{entry.get('subtitle', '')} {entry.get('summary', '')} {details_text}"))
            tf_map: Dict[str, float] = {}
            doc_len = 0
            fields: Dict[str, str] = {
                "title": entry.get("title", ""),
                "symbol": entry.get("symbol", ""),
                "aliases": " ".join(entry.get("aliases", [])),
                "subtitle": entry.get("subtitle", ""),
                "tags": " ".join(entry.get("tags", [])),
                "summary": entry.get("summary", ""),
                "details": details_text,
            }
            for fname, ftext in fields.items():
                weight = self.FIELD_WEIGHTS[fname]
                toks = tokenize(ftext)
                doc_len += len(toks)
                for tok in toks:
                    tf_map[tok] = tf_map.get(tok, 0.0) + weight
            raw_docs.append(tf_map)
            self.doc_norms.append(float(max(doc_len, 1)))
            total_len += max(doc_len, 1)

        avg_len = max(total_len / max(self.n, 1), 1.0)
        k1 = 1.2
        b = 0.45
        df_counts: Dict[str, int] = {}
        for tf_map in raw_docs:
            for tok in tf_map:
                df_counts[tok] = df_counts.get(tok, 0) + 1

        for tok, df in df_counts.items():
            self.idf[tok] = math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

        for idx, tf_map in enumerate(raw_docs):
            norm_factor = (1.0 - b) + b * (self.doc_norms[idx] / avg_len)
            for tok, wtf in tf_map.items():
                bm25_tf = (wtf * (k1 + 1.0)) / (wtf + k1 * norm_factor)
                self.postings.setdefault(tok, []).append((idx, bm25_tf))

    def search(
        self,
        query: str = "",
        categories: Optional[Sequence[str]] = None,
        limit: int = 80,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        cat_set = {c.strip() for c in (categories or []) if c and c.strip() and c.strip() != "all"}
        raw_q = (query or "").strip()
        folded_q = fold_text(raw_q)

        if not folded_q:
            matched = [e for e in self.entries if not cat_set or e["category"] in cat_set]
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 3)
            return {
                "results": copy.deepcopy(matched[:limit]),
                "total": len(matched),
                "elapsed_ms": elapsed_ms,
                "method": "category_filter" if cat_set else "browse_all",
            }

        q_tokens = tokenize(raw_q)
        scores: Dict[int, float] = {}
        matched_q_terms: Dict[int, set] = {}

        # 1. Exact inverted-index BM25F lookup
        for q_idx, tok in enumerate(q_tokens):
            plist = self.postings.get(tok)
            if not plist:
                continue
            idf = self.idf.get(tok, 1.0)
            for doc_idx, bm25_tf in plist:
                if cat_set and self.entries[doc_idx]["category"] not in cat_set:
                    continue
                scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * bm25_tf
                matched_q_terms.setdefault(doc_idx, set()).add(q_idx)

        # 2. Prefix token boost over index keys (e.g. "ljos" -> "ljos", "kalla" -> "kallatil")
        for q_idx, tok in enumerate(q_tokens):
            if len(tok) < 2:
                continue
            for idx_tok, plist in self.postings.items():
                if idx_tok != tok and idx_tok.startswith(tok):
                    idf = self.idf.get(idx_tok, 1.0) * 0.65
                    for doc_idx, bm25_tf in plist:
                        if cat_set and self.entries[doc_idx]["category"] not in cat_set:
                            continue
                        scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * min(bm25_tf, 2.5) * 0.45
                        matched_q_terms.setdefault(doc_idx, set()).add(q_idx)

        # 3. Multi-token coordination & exact phrase/title match bonus
        q_count = max(len(q_tokens), 1)
        for idx in list(scores.keys()):
            if q_count > 1:
                cov = len(matched_q_terms.get(idx, ())) / q_count
                scores[idx] += (cov * cov) * 16.0
            ftitle = self.folded_titles[idx]
            if ftitle == folded_q:
                scores[idx] += 32.0
            elif folded_q in ftitle:
                scores[idx] += 16.0
            elif any(folded_q in al for al in self.folded_aliases[idx]):
                scores[idx] += 12.0
            elif " " in folded_q and folded_q in self.folded_bodies[idx]:
                scores[idx] += 10.0

        # Also check symbol/substring matches even if tokenization missed symbols like ᛋ
        for idx, entry in enumerate(self.entries):
            if cat_set and entry["category"] not in cat_set:
                continue
            if entry.get("symbol") and raw_q in entry["symbol"]:
                scores[idx] = scores.get(idx, 0.0) + 35.0
            elif idx not in scores and len(folded_q) >= 3 and folded_q in self.folded_titles[idx]:
                scores[idx] = 14.0

        # 4. Bigram fuzzy fallback ONLY if zero hits (handles typos like "sowlo" / "ljoz kveka")
        used_fuzzy = False
        if not scores and len(folded_q) >= 3:
            used_fuzzy = True
            q_bigrams = {folded_q[i : i + 2] for i in range(len(folded_q) - 1)}
            for idx, ftitle in enumerate(self.folded_titles):
                if cat_set and self.entries[idx]["category"] not in cat_set:
                    continue
                if len(ftitle) < 2:
                    continue
                t_bigrams = {ftitle[i : i + 2] for i in range(len(ftitle) - 1)}
                inter = len(q_bigrams & t_bigrams)
                if inter:
                    dice = (2.0 * inter) / (len(q_bigrams) + len(t_bigrams))
                    if dice >= 0.35:
                        scores[idx] = dice * 8.0

        ranked_indices = sorted(scores.keys(), key=lambda i: (-scores[i], CATEGORY_ORDER.get(self.entries[i]["category"], 99), i))
        results = [copy.deepcopy(self.entries[i]) for i in ranked_indices[:limit]]
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 3)
        return {
            "results": results,
            "total": len(ranked_indices),
            "elapsed_ms": elapsed_ms,
            "method": "bm25f_fuzzy" if used_fuzzy else "bm25f_inverted_index",
        }


def get_room_codex(playroom_dir: Path, story_bible: Dict[str, Any]) -> Dict[str, Any]:
    """Return full extracted codex entries and category counts for a playroom."""
    import json

    t0 = time.perf_counter()
    fp = _compute_dir_fingerprint(playroom_dir)
    # Strip secrets before building cache key so secret mutations never pollute cache
    public_bible = {k: v for k, v in (story_bible or {}).items() if k != "secrets"}
    if isinstance(public_bible.get("characters"), dict):
        public_bible["characters"] = {
            cid: {ck: cv for ck, cv in prof.items() if ck not in {"secrets", "private_core", "flaw"}}
            if isinstance(prof, dict) else prof
            for cid, prof in public_bible["characters"].items()
        }
    bible_key = json.dumps(public_bible, ensure_ascii=False, sort_keys=True)
    entries = list(_build_cached_codex(str(playroom_dir.resolve()), fp, bible_key))

    counts: Dict[str, int] = {}
    for e in entries:
        counts[e["category"]] = counts.get(e["category"], 0) + 1

    categories = [
        {**cat, "count": counts.get(cat["id"], 0)}
        for cat in CODEX_CATEGORIES
        if counts.get(cat["id"], 0) > 0
    ]
    build_ms = round((time.perf_counter() - t0) * 1000, 3)
    return {
        "story_id": public_bible.get("story_id") or playroom_dir.name,
        "title": public_bible.get("title") or playroom_dir.name,
        "categories": categories,
        "total_entries": len(entries),
        "entries": entries,
        "build_ms": build_ms,
    }
