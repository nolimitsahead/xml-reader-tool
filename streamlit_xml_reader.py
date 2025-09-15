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
# Helpers
# ------------------------------


def _node_path(elem: ET.Element) -> str:
    """Return an approximate path of an element (by tags and position among siblings)."""
    parts = []
    while elem is not None:
        parent = (
            elem.getparent() if hasattr(elem, "getparent") else None
        )  # lxml compat (not used here)
        # For ElementTree (stdlib) we don't have parent: we build path later during traversal.
        # This function is kept for readability; real path is computed in flatten.
        break
    return ""  # path is built during flattening


def _to_safe_text(s: Optional[str]) -> str:
    if s is None:
        return ""
    # Collapse whitespace for display/search friendliness
    return " ".join(s.split())


def _serialize_attrib(attrib: Dict[str, str]) -> str:
    try:
        return json.dumps(attrib, ensure_ascii=False)
    except Exception:
        # fallback plain
        return str(attrib)


def iter_flatten(root: ET.Element) -> List[Dict[str, str]]:
    """
    Flatten XML into a row-per-element table.

    Columns: index, depth, tag, path, attrib(json), text, tail, n_children
    Path uses tag[index] notation (1-based sibling index among same-tag siblings).
    """
    rows: List[Dict[str, str]] = []

    # To compute a stable path without parent pointers, we track sibling counts on the fly
    stack: List[Tuple[ET.Element, int, Dict[str, int], str]] = (
        []
    )  # (elem, depth, sibling_counter, path)

    # We'll implement our own DFS to build the path strings
    def dfs(
        elem: ET.Element, depth: int, parent_path: str, siblings_counter: Dict[str, int]
    ):
        tag = elem.tag
        # increment sibling index for this tag at this level
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

        # For each child, we need a fresh sibling counter for that level
        child_siblings: Dict[str, int] = {}
        for child in list(elem):
            dfs(child, depth + 1, path, child_siblings)

    root_siblings: Dict[str, int] = {}
    dfs(root, 0, "", root_siblings)
    # Add index column after traversal
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
        # attribute JSON contains key
        filt &= df["attrib"].str.contains(rf'"{attr_key}"\s*:', regex=True, na=False)
    if attr_val:
        filt &= df["attrib"].str.contains(attr_val, case=False, na=False)
    if contains_text:
        filt &= df["text"].str.contains(contains_text, case=False, na=False)
    return df[filt]


# ------------------------------
# Sidebar: Options
# ------------------------------
with st.sidebar:
    st.header("⚙️ Tuỳ chọn")
    show_raw = st.toggle("Hiện nội dung XML raw", value=False)
    use_iterparse = st.toggle(
        "Dùng iterparse (file lớn)",
        value=False,
        help="Bật khi file rất lớn để giảm RAM (nhưng mất một số tính năng preview).",
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

# Read file bytes once
content_bytes = uploaded.read()

# Raw preview (optional)
if show_raw:
    st.subheader("🔎 XML Raw (preview)")
    try:
        raw_text = content_bytes.decode("utf-8", errors="replace")
    except Exception:
        raw_text = str(content_bytes[:max_preview_chars])
    st.code(
        raw_text[:max_preview_chars]
        + ("\n…" if len(raw_text) > max_preview_chars else ""),
        language="xml",
    )

# ------------------------------
# Parse XML
# ------------------------------
parse_error = None
root: Optional[ET.Element] = None


def parse_with_elementtree(data: bytes) -> ET.Element:
    return ET.fromstring(data)


try:
    if use_iterparse:
        # Build a tree root in a memory-friendly way; still returns full root to allow flatten
        # Note: stdlib iterparse still builds elements; biggest saving is avoiding string copies
        # For truly huge files, consider lxml iterparse + stream writing. Here we keep stdlib only.
        events = ("start", "end")
        it = ET.iterparse(io.BytesIO(content_bytes), events=events)
        # The root element is obtained on first start event
        for event, elem in it:
            if event == "start" and root is None:
                root = elem
            # On end events, you could clear children to reduce memory, but we need full flatten later
        # root is now filled
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

# ------------------------------
# Flatten & DataFrame
# ------------------------------
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

# ------------------------------
# Search & Filters
# ------------------------------
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

# ------------------------------
# XPath Query (limited ElementTree XPath)
# ------------------------------
st.divider()
st.subheader("🧭 XPath (ElementTree) — tùy chọn")
xpath = st.text_input(
    "Nhập XPath (ví dụ: .//book[@id='x'] | .//patient/name)",
    help="Hỗ trợ subset của XPath theo xml.etree.ElementTree",
)

xpath_results: List[Dict[str, str]] = []
if xpath:
    try:
        matches = root.findall(xpath)
        for m in matches:
            xpath_results.append(
                {
                    "tag": m.tag,
                    "attrib": _serialize_attrib(m.attrib or {}),
                    "text": _to_safe_text(m.text),
                    "n_children": len(list(m)),
                }
            )
        st.info(f"XPath trả về {len(xpath_results):,} node")
        st.dataframe(pd.DataFrame(xpath_results).head(2000), use_container_width=True)
    except SyntaxError as e:
        st.error(f"XPath SyntaxError: {e}")
    except Exception as e:
        st.error(f"XPath error: {e}")

# ------------------------------
# Downloads
# ------------------------------
st.divider()
st.subheader("📥 Xuất dữ liệu")

# JSON (full table)
json_bytes = df.to_json(orient="records", force_ascii=False).encode("utf-8")
st.download_button(
    "Tải JSON (toàn bộ)",
    data=json_bytes,
    file_name="xml_flatten.json",
    mime="application/json",
)

# CSV (filtered)
csv_bytes = filtered.to_csv(index=False).encode("utf-8")
st.download_button(
    "Tải CSV (bảng đã lọc)",
    data=csv_bytes,
    file_name="xml_filtered.csv",
    mime="text/csv",
)

# Small preview of tree paths (for quick orientation)
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
