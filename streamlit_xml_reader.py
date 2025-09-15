import io
import json
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

st.set_page_config(page_title="XML Reader • Streamlit", page_icon="📦", layout="wide")
st.title("📦 XML Reader & Explorer")
st.caption(
    "Tải XML lên, xem cấu trúc, tìm kiếm theo tag/attribute/text, lọc bằng XPath, và xuất CSV/JSON."
)


# ------------------------------
# Helpers (dùng cho XML thường)
# ------------------------------
def _to_safe_text(s: Optional[str]) -> str:
    if s is None:
        return ""
    return " ".join(s.split())


def _serialize_attrib(attrib: Dict[str, str]) -> str:
    try:
        return json.dumps(attrib, ensure_ascii=False)
    except Exception:
        return str(attrib)


def iter_flatten(root: ET.Element) -> List[Dict[str, str]]:
    """Flatten XML thường thành bảng row-per-element."""
    rows: List[Dict[str, str]] = []

    def dfs(
        elem: ET.Element, depth: int, parent_path: str, siblings_counter: Dict[str, int]
    ):
        tag = elem.tag
        idx = siblings_counter.get(tag, 0) + 1
        siblings_counter[tag] = idx
        path = f"{parent_path}/{tag}[{idx}]" if parent_path else f"/{tag}[{idx}]"

        row = {
            "depth": depth,
            "tag": tag,
            "path": path,
            "attrib": _serialize_attrib(elem.attrib or {}),
            "text": _to_safe_text(elem.text),
            "tail": _to_safe_text(elem.tail),
            "n_children": len(list(elem)),
        }
        rows.append(row)

        child_siblings: Dict[str, int] = {}
        for child in list(elem):
            dfs(child, depth + 1, path, child_siblings)

    dfs(root, 0, "", {})
    for i, r in enumerate(rows):
        r["index"] = i
    return rows


def search_rows(
    df: pd.DataFrame, tag: str, attr_key: str, attr_val: str, contains_text: str
) -> pd.DataFrame:
    filt = pd.Series([True] * len(df))
    if tag:
        filt &= df["tag"].str.contains(tag, case=False, na=False)
    if attr_key:
        filt &= df["attrib"].str.contains(rf'"{attr_key}"\s*:', regex=True, na=False)
    if attr_val:
        filt &= df["attrib"].str.contains(attr_val, case=False, na=False)
    if contains_text:
        filt &= df["text"].str.contains(contains_text, case=False, na=False)
    return df[filt]


# ------------------------------
# Helpers (mới) cho Excel XML (SpreadsheetML)
# ------------------------------
SS_NS = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}


def is_spreadsheetml_xml(content: bytes) -> bool:
    # Nhan dien nhanh: có namespace Excel SpreadsheetML
    return (
        b"urn:schemas-microsoft-com:office:spreadsheet" in content
        or b"mso-application" in content
    )


def _rows_from_spreadsheetml_table(table: ET.Element) -> List[List[object]]:
    """Đọc ss:Table → list các hàng (đã chèn None theo ss:Index)."""
    rows: List[List[object]] = []
    for row in table.findall("ss:Row", SS_NS):
        vals: List[object] = []
        col_i = 1  # 1-based
        for cell in row.findall("ss:Cell", SS_NS):
            idx = cell.attrib.get("{urn:schemas-microsoft-com:office:spreadsheet}Index")
            if idx is not None:
                idx = int(float(idx))
                while col_i < idx:
                    vals.append(None)
                    col_i += 1
            data = cell.find("ss:Data", SS_NS)
            if data is None:
                vals.append(None)
            else:
                txt = data.text
                typ = data.attrib.get(
                    "{urn:schemas-microsoft-com:office:spreadsheet}Type", "String"
                )
                if typ == "Number":
                    try:
                        vals.append(float(txt))
                    except Exception:
                        vals.append(txt)
                elif typ == "DateTime":
                    try:
                        vals.append(pd.to_datetime(txt))
                    except Exception:
                        vals.append(txt)
                else:
                    vals.append(txt)
            col_i += 1
        rows.append(vals)
    # chuẩn hóa độ dài
    width = max((len(r) for r in rows), default=0)
    rows = [r + [None] * (width - len(r)) for r in rows]
    return rows


def _pick_header_index(
    rows: List[List[object]], min_non_null: int = 3
) -> Optional[int]:
    """Chọn dòng header: dòng đầu tiên có >= min_non_null ô không rỗng."""
    for i, r in enumerate(rows):
        nn = sum(x is not None and str(x).strip() != "" for x in r)
        if nn >= min_non_null:
            return i
    return None


def parse_spreadsheetml(
    content: bytes,
    header_mode: str = "auto",
    header_row_manual: int = 1,
    min_non_null_header: int = 3,
) -> Dict[str, pd.DataFrame]:
    """Trả về dict[WorksheetName] = DataFrame."""
    root = ET.fromstring(content)
    dfs: Dict[str, pd.DataFrame] = {}
    for ws in root.findall(".//ss:Worksheet", SS_NS):
        name = ws.attrib.get(
            "{urn:schemas-microsoft-com:office:spreadsheet}Name", "Sheet"
        )
        table = ws.find(".//ss:Table", SS_NS)
        if table is None:
            continue
        rows = _rows_from_spreadsheetml_table(table)
        if not rows:
            dfs[name] = pd.DataFrame()
            continue

        if header_mode == "manual":
            hdr_idx = max(0, header_row_manual - 1)
        else:
            hdr_idx = _pick_header_index(rows, min_non_null=min_non_null_header)

        if hdr_idx is None:
            df = pd.DataFrame(rows)
        else:
            header = [
                str(c).strip() if c is not None else f"col{j+1}"
                for j, c in enumerate(rows[hdr_idx])
            ]
            data = rows[hdr_idx + 1 :]
            df = pd.DataFrame(data, columns=header)

        df = df.dropna(how="all").dropna(axis=1, how="all")
        dfs[name] = df
    return dfs


# ------------------------------
# Sidebar
# ------------------------------
with st.sidebar:
    st.header("⚙️ Tuỳ chọn")
    show_raw = st.toggle("Hiện nội dung XML raw", value=False)
    use_iterparse = st.toggle(
        "Dùng iterparse (file lớn)",
        value=False,
        help="Bật khi file rất lớn để giảm RAM (XML thường).",
    )
    # Tuỳ chọn riêng cho SpreadsheetML
    st.markdown("**Excel XML (SpreadsheetML)**")
    spreadsheetml_header_mode = st.radio(
        "Header mode", ["auto", "manual"], index=0, help="Dành cho Excel XML"
    )
    spreadsheetml_header_row = st.number_input(
        "Header row (1-based, manual)", min_value=1, value=1, step=1
    )
    spreadsheetml_min_nonnull = st.number_input(
        "Min non-null cells to detect header (auto)", min_value=1, value=3, step=1
    )
    max_preview_chars = st.number_input(
        "Giới hạn ký tự preview", min_value=200, max_value=20000, value=2000, step=200
    )

# ------------------------------
# File upload
# ------------------------------
uploaded = st.file_uploader("📂 Chọn file XML", type=["xml"])
if uploaded is None:
    st.info("⬆️ Tải một file .xml để bắt đầu.")
    st.stop()

content_bytes = uploaded.read()

# Raw preview
if show_raw:
    st.subheader("🔎 XML Raw (preview)")
    raw_text = content_bytes.decode("utf-8", errors="replace")
    st.code(
        raw_text[:max_preview_chars]
        + ("\n…" if len(raw_text) > max_preview_chars else ""),
        language="xml",
    )

# ------------------------------
# Branch: SpreadsheetML hay XML thường?
# ------------------------------
if is_spreadsheetml_xml(content_bytes):
    st.success(
        "🧾 Phát hiện Excel 2003 XML (SpreadsheetML). Đang parse theo Worksheet/Table/Row/Cell…"
    )

    # Parse SpreadsheetML → dict of DataFrames
    dfs = parse_spreadsheetml(
        content_bytes,
        header_mode=spreadsheetml_header_mode,
        header_row_manual=int(spreadsheetml_header_row),
        min_non_null_header=int(spreadsheetml_min_nonnull),
    )

    # Tổng quan sheet
    info_rows = []
    for name, df in dfs.items():
        info_rows.append({"Worksheet": name, "Rows": df.shape[0], "Cols": df.shape[1]})
    st.subheader("📑 Tổng quan Worksheet")
    st.dataframe(pd.DataFrame(info_rows), use_container_width=True)

    # Chọn sheet để xem
    sheet_names = list(dfs.keys())
    if not sheet_names:
        st.warning("Không tìm thấy Table nào trong file.")
        st.stop()

    sel = st.selectbox("Chọn Worksheet để xem", options=sheet_names, index=0)
    df_show = dfs[sel]
    st.dataframe(df_show.head(2000), use_container_width=True)

    # Download nút: per-sheet & combined
    st.subheader("📥 Xuất CSV")
    colA, colB = st.columns(2)
    with colA:
        st.download_button(
            f"Tải CSV – {sel}",
            data=df_show.to_csv(index=False).encode("utf-8"),
            file_name=f"{sel}.csv",
            mime="text/csv",
        )
    with colB:
        combined = pd.concat(
            [d.assign(Worksheet=n) for n, d in dfs.items()], ignore_index=True
        )
        st.download_button(
            "Tải CSV – combined (tất cả sheet)",
            data=combined.to_csv(index=False).encode("utf-8"),
            file_name="spreadsheetml_combined.csv",
            mime="text/csv",
        )

    st.caption(
        "💡 Mẹo: Nếu header auto chưa đúng, chuyển sang 'manual' và đặt 'Header row' cho chuẩn."
    )

else:
    # XML thường: chạy pipeline cũ (flatten + search + XPath)
    st.info("🧩 XML thường (không phải SpreadsheetML). Sử dụng chế độ Flatten + XPath.")
    parse_error = None
    root: Optional[ET.Element] = None

    def parse_with_elementtree(data: bytes) -> ET.Element:
        return ET.fromstring(data)

    try:
        if use_iterparse:
            events = ("start", "end")
            it = ET.iterparse(io.BytesIO(content_bytes), events=events)
            for event, elem in it:
                if event == "start" and root is None:
                    root = elem
            if root is None:
                raise ET.ParseError("Không tìm thấy root element")
        else:
            root = parse_with_elementtree(content_bytes)
    except ET.ParseError as e:
        parse_error = f"XML ParseError: {e}"
    except Exception as e:
        parse_error = f"Lỗi khi đọc XML: {e}"

    if parse_error:
        st.error(parse_error)
        st.stop()
    assert root is not None

    with st.spinner("Đang chuyển XML thành bảng…"):
        rows = iter_flatten(root)
        df = (
            pd.DataFrame(
                rows,
                columns=[
                    "index",
                    "depth",
                    "tag",
                    "path",
                    "attrib",
                    "text",
                    "tail",
                    "n_children",
                ],
            )
            .sort_values("index")
            .reset_index(drop=True)
        )

    st.success(f"✅ Đã parse xong: {len(df):,} elements")

    st.subheader("🔍 Tìm kiếm / Lọc")
    col1, col2, col3, col4, col5 = st.columns([1, 1, 1, 2, 1])
    with col1:
        q_tag = st.text_input("Tag contains", placeholder="e.g. item | patient | book")
    with col2:
        q_attr_key = st.text_input("Attribute key", placeholder="e.g. id | name")
    with col3:
        q_attr_val = st.text_input(
            "Attribute value contains", placeholder="e.g. 123 | John"
        )
    with col4:
        q_text = st.text_input(
            "Text contains", placeholder="e.g. Hà Nội | Warsaw | Stockholm"
        )
    with col5:
        max_rows = st.number_input(
            "Hiển thị tối đa", min_value=50, max_value=100000, value=2000, step=50
        )

    filtered = search_rows(
        df, q_tag.strip(), q_attr_key.strip(), q_attr_val.strip(), q_text.strip()
    )
    st.caption(f"Kết quả: {len(filtered):,} hàng")
    st.dataframe(filtered.head(int(max_rows)), use_container_width=True)

    st.divider()
    st.subheader("🧭 XPath (ElementTree) — tùy chọn")
    xpath = st.text_input(
        "Nhập XPath (ví dụ: .//book[@id='x'] | .//patient/name)",
        help="Hỗ trợ subset của XPath theo xml.etree.ElementTree",
    )
    if xpath:
        try:
            matches = root.findall(xpath)
            rows_x = [
                {
                    "tag": m.tag,
                    "attrib": _serialize_attrib(m.attrib or {}),
                    "text": _to_safe_text(m.text),
                    "n_children": len(list(m)),
                }
                for m in matches
            ]
            st.info(f"XPath trả về {len(rows_x):,} node")
            st.dataframe(pd.DataFrame(rows_x).head(2000), use_container_width=True)
        except SyntaxError as e:
            st.error(f"XPath SyntaxError: {e}")
        except Exception as e:
            st.error(f"XPath error: {e}")

    st.divider()
    st.subheader("📥 Xuất dữ liệu")
    st.download_button(
        "Tải JSON (toàn bộ)",
        data=df.to_json(orient="records", force_ascii=False).encode("utf-8"),
        file_name="xml_flatten.json",
        mime="application/json",
    )
    st.download_button(
        "Tải CSV (bảng đã lọc)",
        data=filtered.to_csv(index=False).encode("utf-8"),
        file_name="xml_filtered.csv",
        mime="text/csv",
    )

    st.divider()
    st.subheader("🌳 Tree preview (đường dẫn)")
    preview_depth = st.slider(
        "Độ sâu tối đa",
        min_value=0,
        max_value=int(df["depth"].max()),
        value=min(3, int(df["depth"].max())),
    )
    preview_df = df[df["depth"] <= preview_depth][
        ["index", "depth", "path", "tag", "n_children"]
    ].head(3000)
    st.dataframe(preview_df, use_container_width=True)
    st.caption(
        "💡 Mẹo: dùng cột `path` để định vị node khi xử lý downstream (ví dụ: /root[1]/patient[12]/name[1])."
    )
