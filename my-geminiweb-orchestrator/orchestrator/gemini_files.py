"""Extractor tổng quát: lấy file Gemini sinh ra từ raw candidate.

Cấu trúc (đã verify trên account thật, 2026-11-14):

    candidate[12][0]["59"][0][0]
      [0] = "[file-tag: code-generated-file-<uuid>]"
      [2] = [None, 3, <filename>, None, None, <base64 blob>, None,
             [thumb_url, download_url, upload_url]]
              [2][2]  = tên file, ví dụ "Bao_cao_2026.pdf"
              [2][5]  = base64 nội dung file (nếu có)
              [2][7][1] = URL tải contribution.usercontent.google.com/download

Có thể có nhiều file -> trả về list.
"""

from __future__ import annotations

import base64
import re
from typing import Any


def extract_generated_files(candidate_data: Any) -> list[dict[str, Any]]:
    """Quét raw candidate, trả về danh sách file Gemini đã sinh.

    Mỗi phần tử: ``{"filename", "download_url", "thumb_url", "upload_url",
    "mime_type", "blob_b64", "tag"}``.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(node: Any, path: str = "") -> None:
        if isinstance(node, list):
            for i, v in enumerate(node):
                visit(v, f"{path}[{i}]")
        elif isinstance(node, dict):
            for k, v in node.items():
                visit(v, f"{path}.{k}")
        elif isinstance(node, str) and node.startswith("[file-tag:"):
            tag = node
            if tag in seen:
                return
            seen.add(tag)
            # Đi ngược: field 59 -> [0][0] -> [2]
            parent = _find_parent(root, node)
            info: dict[str, Any] = {"tag": tag}
            if parent:
                blob = parent[2]
                if isinstance(blob, list) and len(blob) > 2:
                    info["filename"] = blob[2]
                    if len(blob) > 5 and isinstance(blob[5], str):
                        info["blob_b64"] = blob[5]
                    if len(blob) > 7 and isinstance(blob[7], list):
                        urls = blob[7]
                        info["thumb_url"] = urls[0] if len(urls) > 0 else None
                        info["download_url"] = urls[1] if len(urls) > 1 else None
                        info["upload_url"] = urls[2] if len(urls) > 2 else None
                    if len(blob) > 3:
                        info["mime_type"] = blob[3]
            out.append(info)

    root = candidate_data
    visit(root)
    return out


def _find_parent(root: Any, tag: str) -> list | None:
    """Tìm list cha chứa `tag` tại vị trí [0] của node có [2] là list."""
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            if node and node[0] == tag and len(node) > 2 and isinstance(node[2], list):
                return node
            for v in node:
                stack.append(v)
        elif isinstance(node, dict):
            for v in node.values():
                stack.append(v)
    return None


def decode_blob(info: dict[str, Any]) -> bytes | None:
    """Giải mã base64 blob nếu có (không cần network)."""
    b64 = info.get("blob_b64")
    if not b64:
        return None
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


def guess_mime(filename: str) -> str:
    import mimetypes

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def sanitize_filename(name: str | None, fallback: str = "gemini_output") -> str:
    if not name:
        return fallback
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()
    return name or fallback
