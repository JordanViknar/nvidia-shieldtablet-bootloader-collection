#!/usr/bin/env python3
"""Extract top-level components from NVIDIA SHIELD Tablet/K1 update blobs.

Usage:
    python3 extract_shield_blob_components.py /path/to/blob
    python3 extract_shield_blob_components.py /path/to/blob --list
    python3 extract_shield_blob_components.py /path/to/blob --force

Important format detail discovered by comparing SHIELD Tablet/K1 releases:
The component table's stored offsets are 0x1c (28) bytes before the actual
payload start. This applies to *all* components, not only BCT. The script
infers that bias from the NVC entry/table end, then validates it using the
DTB FDT headers and Tegra124 BCT structure before extracting anything.

A component occurring once is saved under its tag (e.g. NVC, WB0, BCT).
Repeated tags are numbered in table order (e.g. DTB_01, DTB_02, ...).
When two tags alias the same range (EBT and RBL in these blobs), both files
are written; their contents will be identical.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SIGNED_MAGIC = b"-SIGNED-BY-SIGNBLOB-"
BLOB_MAGIC = b"MSM-RADIO-UPDATE"  # 16 bytes, at 0x1c
BLOB_VERSION = 0x00010000
NUM_PARTS_OFFSET = 0x38
TABLE_OFFSET = 0x58
ENTRY = struct.Struct("<4sIII")  # tag, stored offset, payload size, entry version

# Tegra124 BCT sanity checks, relative to the actual BCT payload start.
BCT_LENGTH = 0x2000
BCT_EXPECTED = {
    0x6D0: 0x00400001,       # BCT version
    0x6D4: 14,               # log2(block size) => 0x4000
    0x6D8: 9,                # log2(page size) => 0x200
    0x6DC: 0x08000000,       # partition size
}
FDT_MAGIC = b"\xd0\x0d\xfe\xed"


@dataclass(frozen=True)
class Component:
    tag: str
    stored_offset: int
    size: int
    version: int
    table_index: int


@dataclass(frozen=True)
class LocatedComponent:
    component: Component
    payload_offset: int
    payload: bytes
    filename: str


def read_u32_le(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def read_u32_be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def parse_components(data: bytes) -> tuple[list[Component], int, int]:
    """Return (components, table_start, table_end) after validating the header."""
    if len(data) < TABLE_OFFSET:
        raise ValueError("File is too small to contain the blob header")
    if data[: len(SIGNED_MAGIC)] != SIGNED_MAGIC:
        raise ValueError("Missing -SIGNED-BY-SIGNBLOB- header signature")
    if data[0x1C:0x1C + len(BLOB_MAGIC)] != BLOB_MAGIC:
        raise ValueError("Missing MSM-RADIO-UPDATE blob magic")

    version = read_u32_le(data, 0x2C)
    if version != BLOB_VERSION:
        raise ValueError(f"Unsupported blob header version 0x{version:08X}")

    count = read_u32_le(data, NUM_PARTS_OFFSET)
    if count == 0:
        raise ValueError("Blob header declares zero components")
    table_end = TABLE_OFFSET + count * ENTRY.size
    if table_end > len(data):
        raise ValueError(
            f"Component table ends at 0x{table_end:X}, beyond file size 0x{len(data):X}"
        )

    components: list[Component] = []
    for index in range(count):
        entry_pos = TABLE_OFFSET + index * ENTRY.size
        raw_tag, offset, size, entry_version = ENTRY.unpack_from(data, entry_pos)
        raw_tag = raw_tag.split(b"\0", 1)[0]
        try:
            tag = raw_tag.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Non-ASCII tag in table entry {index} at 0x{entry_pos:X}") from exc
        if not tag or not re.fullmatch(r"[A-Za-z0-9_.-]+", tag):
            raise ValueError(f"Invalid component tag {tag!r} in table entry {index}")
        if size == 0:
            raise ValueError(f"Component {tag} in table entry {index} has zero size")
        components.append(Component(tag, offset, size, entry_version, index))

    return components, TABLE_OFFSET, table_end


def is_t124_bct(data: bytes, start: int) -> bool:
    if start < 0 or start + BCT_LENGTH > len(data):
        return False
    return all(read_u32_le(data, start + rel) == expected for rel, expected in BCT_EXPECTED.items())


def detect_offset_bias(data: bytes, components: list[Component], table_end: int) -> int:
    """Infer the shared stored-offset -> payload-offset adjustment.

    In these SHIELD Tablet/K1 blobs, NVC's actual payload begins immediately
    after the component table. Thus table_end - NVC.stored_offset gives the
    common bias (0x1c in all nine supplied releases). If NVC is absent, fall
    back to locating a valid Tegra124 BCT within a bounded window.
    """
    nvc_entries = [c for c in components if c.tag == "NVC"]
    if len(nvc_entries) == 1:
        bias = table_end - nvc_entries[0].stored_offset
        if 0 <= bias <= 0x1000:
            return bias
        raise ValueError(
            "Cannot infer component offset bias from NVC: "
            f"table ends at 0x{table_end:X}, NVC offset is 0x{nvc_entries[0].stored_offset:X}"
        )

    bct_entries = [c for c in components if c.tag == "BCT"]
    if len(bct_entries) == 1 and bct_entries[0].size == BCT_LENGTH:
        bct = bct_entries[0]
        search_end = min(bct.stored_offset + 0x101, len(data) - BCT_LENGTH + 1)
        for candidate in range(bct.stored_offset, search_end):
            if is_t124_bct(data, candidate):
                return candidate - bct.stored_offset

    raise ValueError(
        "Could not infer the component offset bias. Expected one NVC entry, "
        "or a recognizable 8 KiB Tegra124 BCT."
    )


def output_filenames(components: list[Component]) -> list[str]:
    counts = Counter(c.tag for c in components)
    seen: Counter[str] = Counter()
    names: list[str] = []
    for component in components:
        seen[component.tag] += 1
        if counts[component.tag] == 1:
            names.append(component.tag)
        else:
            names.append(f"{component.tag}_{seen[component.tag]:02d}")
    return names


def validate_and_locate(
    data: bytes,
    components: list[Component],
    table_end: int,
    bias: int,
    filenames: list[str],
) -> list[LocatedComponent]:
    """Apply the bias and validate bounds plus available format signatures."""
    located: list[LocatedComponent] = []
    for component, filename in zip(components, filenames):
        start = component.stored_offset + bias
        end = start + component.size
        if start < 0 or end > len(data) or end < start:
            raise ValueError(
                f"{component.tag} entry {component.table_index}: adjusted range "
                f"0x{start:X}..0x{end:X} falls outside file size 0x{len(data):X}"
            )
        located.append(LocatedComponent(component, start, data[start:end], filename))

    # This relation is true in the supplied package family and is a useful
    # sanity check that the detected shift doesn't leave the NVC in the header.
    nvc_entries = [item for item in located if item.component.tag == "NVC"]
    if nvc_entries and len(nvc_entries) == 1 and nvc_entries[0].payload_offset != table_end:
        raise ValueError(
            f"NVC payload starts at 0x{nvc_entries[0].payload_offset:X}, "
            f"but component table ends at 0x{table_end:X}; offset bias may be wrong"
        )

    dtbs = [item for item in located if item.component.tag == "DTB"]
    if dtbs:
        for item in dtbs:
            if len(item.payload) < 8 or item.payload[:4] != FDT_MAGIC:
                raise ValueError(
                    f"{item.filename} does not begin with an FDT magic at adjusted "
                    f"offset 0x{item.payload_offset:X}; component offset bias may be wrong"
                )
            total_size = read_u32_be(item.payload, 4)
            if total_size != item.component.size:
                raise ValueError(
                    f"{item.filename} FDT totalsize is 0x{total_size:X}, while its table "
                    f"size is 0x{item.component.size:X}; refusing a likely misaligned extraction"
                )

    bcts = [item for item in located if item.component.tag == "BCT"]
    if bcts:
        if len(bcts) != 1:
            raise ValueError(f"Expected one BCT entry, found {len(bcts)}")
        item = bcts[0]
        if item.component.size != BCT_LENGTH or not is_t124_bct(item.payload, 0):
            raise ValueError(
                f"BCT at adjusted offset 0x{item.payload_offset:X} did not pass Tegra124 "
                "sanity checks; refusing a likely misaligned extraction"
            )

    if not dtbs and not bcts:
        raise ValueError("No DTB or BCT entries available to validate the inferred offset bias")

    # Exact aliases are legitimate (EBT and RBL point at the same bytes here).
    # Reject partial overlaps, which are much more likely to indicate a bad parse.
    ranges: dict[tuple[int, int], list[str]] = {}
    for item in located:
        ranges.setdefault((item.payload_offset, item.payload_offset + item.component.size), []).append(
            item.filename
        )
    unique_ranges = sorted(ranges)
    for previous, current in zip(unique_ranges, unique_ranges[1:]):
        if current[0] < previous[1]:
            raise ValueError(
                "Adjusted component ranges partially overlap: "
                f"{', '.join(ranges[previous])} and {', '.join(ranges[current])}"
            )

    return located


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract all top-level components from an NVIDIA SHIELD Tablet/K1 blob."
    )
    parser.add_argument("blob", type=Path, help="path to the input blob")
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    parser.add_argument("--list", action="store_true", dest="list_only", help="list without writing files")
    parser.add_argument(
        "--offset-bias",
        type=lambda value: int(value, 0),
        default=None,
        help="override auto-detected offset bias (integer, e.g. 0x1c); payload checks still apply",
    )
    args = parser.parse_args()

    blob_path = args.blob.expanduser().resolve()
    if not blob_path.is_file():
        print(f"error: input file does not exist: {blob_path}", file=sys.stderr)
        return 2

    try:
        data = blob_path.read_bytes()
        components, table_start, table_end = parse_components(data)
        bias = args.offset_bias
        if bias is None:
            bias = detect_offset_bias(data, components, table_end)
        if bias < 0:
            raise ValueError("Offset bias must be nonnegative")

        filenames = output_filenames(components)
        located = validate_and_locate(data, components, table_end, bias, filenames)

        print(f"Input:          {blob_path}")
        print(f"File size:      {len(data)} bytes (0x{len(data):X})")
        print(
            f"Component table: 0x{table_start:X}..0x{table_end - 1:X} "
            f"({len(components)} entries)"
        )
        print(f"Offset bias:    +0x{bias:X} (stored offsets point before payload starts)")
        print()

        # Check all output paths before writing any component.
        output_paths: list[Path] = []
        for item in located:
            out_path = blob_path.parent / item.filename
            if out_path.resolve() == blob_path:
                raise ValueError(f"Refusing to overwrite input blob with output {out_path}")
            if not args.list_only and out_path.exists() and not args.force:
                raise FileExistsError(f"output already exists: {out_path} (use --force to overwrite)")
            output_paths.append(out_path)

        alias_groups: dict[tuple[int, int], list[str]] = {}
        for item in located:
            key = (item.payload_offset, item.payload_offset + item.component.size)
            alias_groups.setdefault(key, []).append(item.filename)

        for item in located:
            comp = item.component
            end = item.payload_offset + comp.size
            digest = hashlib.sha256(item.payload).hexdigest()[:12]
            alias = alias_groups[(item.payload_offset, end)]
            alias_note = f"; alias: {', '.join(alias)}" if len(alias) > 1 else ""
            print(
                f"{item.filename:8} entry={comp.table_index:02d} "
                f"stored=0x{comp.stored_offset:X} payload=0x{item.payload_offset:X} "
                f"size={comp.size:>8} (0x{comp.size:X}) sha256={digest}{alias_note}"
            )

        if args.list_only:
            return 0

        for item, out_path in zip(located, output_paths):
            out_path.write_bytes(item.payload)
            print(f"Wrote {out_path}")

    except (OSError, ValueError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
