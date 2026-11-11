"""core/diskinfo.py 与 core/smart.py 提权辅助的单元测试。

纯函数测试用构造的 bytes，不触碰硬件；只有文件末尾的冒烟测试会真的枚举本机盘，
且不要求管理员权限（拿不到时返回空列表，本身不报错）。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import diskinfo  # noqa: E402
from core import smart  # noqa: E402


def build_descriptor(vendor: str, product: str, bus_type: int) -> bytes:
    """构造一个 STORAGE_DEVICE_DESCRIPTOR：36 字节头 + NUL 结尾的 vendor/product 字符串。"""
    body = bytearray(b"\x00" * 36)
    strings = bytearray()
    vendor_off = 0
    product_off = 0
    if vendor:
        vendor_off = 36 + len(strings)
        strings += vendor.encode("ascii") + b"\x00"
    if product:
        product_off = 36 + len(strings)
        strings += product.encode("ascii") + b"\x00"
    struct.pack_into("<I", body, 4, 36 + len(strings))      # Size
    struct.pack_into("<I", body, 12, vendor_off)            # VendorIdOffset
    struct.pack_into("<I", body, 16, product_off)           # ProductIdOffset
    struct.pack_into("<I", body, 28, bus_type)              # BusType
    return bytes(body) + bytes(strings)


# ---------------------------------------------------------------- 盘名拼接

def test_compose_name_joins_vendor_and_product():
    assert diskinfo.compose_name("INTEL", "SSDPEKNW010T8") == "INTEL SSDPEKNW010T8"


def test_compose_name_empty_vendor_uses_product_only():
    assert diskinfo.compose_name("", "WDC WD10EZEX") == "WDC WD10EZEX"


def test_compose_name_vendor_equal_to_product_dedupes():
    assert diskinfo.compose_name("Samsung", "Samsung") == "Samsung"


def test_compose_name_product_already_contains_vendor_dedupes():
    # product 已含 vendor 时不再重复拼接，避免 "Samsung Samsung SSD 980"
    assert diskinfo.compose_name("SAMSUNG", "Samsung SSD 980 PRO") == "Samsung SSD 980 PRO"


def test_compose_name_strips_whitespace():
    assert diskinfo.compose_name("  INTEL  ", "  SSDPEKNW010T8 ") == "INTEL SSDPEKNW010T8"


def test_compose_name_empty_product_falls_back_to_vendor():
    assert diskinfo.compose_name("INTEL", "") == "INTEL"
    assert diskinfo.compose_name("", "") == ""


# ---------------------------------------------------------------- descriptor 偏移解析

def test_parse_descriptor_reads_vendor_product_and_bus():
    buf = build_descriptor("INTEL", "SSDPEKNW010T8", 17)
    assert diskinfo.parse_storage_descriptor(buf) == ("INTEL", "SSDPEKNW010T8", 17)


def test_parse_descriptor_missing_vendor_offset_is_empty():
    buf = build_descriptor("", "ST1000DM010", 11)
    assert diskinfo.parse_storage_descriptor(buf) == ("", "ST1000DM010", 11)


def test_parse_descriptor_strips_trailing_padding_spaces():
    buf = build_descriptor("ATA     ", "ST1000DM010   ", 11)
    vendor, product, bus = diskinfo.parse_storage_descriptor(buf)
    assert vendor == "ATA"
    assert product == "ST1000DM010"
    assert bus == 11


def test_parse_descriptor_truncated_buffer_returns_defaults():
    assert diskinfo.parse_storage_descriptor(b"\x00" * 10) == ("", "", 0)
    assert diskinfo.parse_storage_descriptor(b"") == ("", "", 0)


def test_parse_descriptor_offset_out_of_range_is_empty():
    buf = bytearray(build_descriptor("", "X", 7))
    struct.pack_into("<I", buf, 16, 9999)   # ProductIdOffset 越界
    assert diskinfo.parse_storage_descriptor(bytes(buf)) == ("", "", 7)


def test_parse_seek_penalty_true_false_and_short():
    assert diskinfo.parse_seek_penalty(struct.pack("<IIB", 9, 9, 1)) is True
    assert diskinfo.parse_seek_penalty(struct.pack("<IIB", 9, 9, 0)) is False
    assert diskinfo.parse_seek_penalty(b"\x00" * 4) is None


def test_parse_length_little_endian_and_short():
    assert diskinfo.parse_length(struct.pack("<q", 1000204886016)) == 1000204886016
    assert diskinfo.parse_length(b"\x00\x00") is None


# ---------------------------------------------------------------- 介质判定

def test_media_nvme_wins_regardless_of_seek_penalty():
    assert diskinfo.media_type(17, False) == "NVMe"
    assert diskinfo.media_type(17, None) == "NVMe"


def test_media_hdd_and_ssd_from_seek_penalty():
    assert diskinfo.media_type(11, True) == "HDD"
    assert diskinfo.media_type(11, False) == "SSD"


def test_media_unknown_when_no_seek_info():
    assert diskinfo.media_type(7, None) == ""


# ---------------------------------------------------------------- 枚举遇空号继续

def test_collect_disks_skips_holes_and_keeps_going():
    present = {
        0: {"device_id": "0", "name": "A"},
        2: {"device_id": "2", "name": "C"},
        5: {"device_id": "5", "name": "F"},
    }
    probed: list[int] = []

    def fake_probe(i: int):
        probed.append(i)
        return present.get(i)

    out = diskinfo.collect_disks(fake_probe, max_index=8)
    assert [d["device_id"] for d in out] == ["0", "2", "5"]
    assert probed == list(range(8))          # 空号之后还继续探测


def test_collect_disks_probe_exception_treated_as_hole():
    def bad_probe(i: int):
        if i == 1:
            raise OSError("boom")
        return {"device_id": str(i)}

    out = diskinfo.collect_disks(bad_probe, max_index=3)
    assert [d["device_id"] for d in out] == ["0", "2"]


def test_collect_disks_all_holes_returns_empty():
    assert diskinfo.collect_disks(lambda i: None, max_index=32) == []


# ---------------------------------------------------------------- 提权用 pythonw

def test_gui_python_prefers_pythonw_in_same_dir(tmp_path):
    py = tmp_path / "python.exe"
    pw = tmp_path / "pythonw.exe"
    py.write_bytes(b"")
    pw.write_bytes(b"")
    assert smart._gui_python(str(py)) == str(pw)


def test_gui_python_keeps_exe_when_no_pythonw(tmp_path):
    py = tmp_path / "python.exe"
    py.write_bytes(b"")
    assert smart._gui_python(str(py)) == str(py)


def test_gui_python_leaves_non_python_exe_alone(tmp_path):
    other = tmp_path / "app.exe"
    other.write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert smart._gui_python(str(other)) == str(other)


# ---------------------------------------------------------------- 冒烟（不需要管理员）

def test_list_disks_smoke_never_raises():
    """普通权限下可能拿不到盘，但必须返回 list 而不是抛异常；键结构与旧 PowerShell 输出兼容。"""
    disks = diskinfo.list_disks()
    assert isinstance(disks, list)
    for d in disks:
        assert set(d) >= {"name", "device_id", "media_type", "size_bytes", "health"}
        assert d["device_id"].isdigit()
        assert isinstance(d["size_bytes"], int)
