# NVIDIA SHIELD Tablet bootloader blobs

This repository collects NVIDIA SHIELD Tablet and SHIELD Tablet K1 update blobs, organized by device variant and firmware release. It also includes a Python script for extracting the top-level components from each blob.

## Repository layout

The four directories correspond to the supplied firmware collections:

| Directory | Device / variant | Releases included |
|---|---|---|
| `sb-na-wf/` | SHIELD Tablet K1 | `0.0.0`, `1.1.1`, `1.2.0`, `1.3.0`, `1.4.0`, `1.5.0`, `5.0.0`, `5.1.0`, `5.2.0` |
| `wx-na-wf/` | SHIELD Tablet Wi-Fi | `3.1.1`, `4.0.1`, `4.1.0`, `4.2.0`, `4.3.0`, `4.4.0`, `5.0.0`, `5.1.0`, `5.2.0` |
| `wx-na-do/` | SHIELD Tablet LTE (USA) | `3.1.1`, `4.1.0`, `4.4.0` |
| `wx-un-do/` | SHIELD Tablet LTE (Rest of World) | `3.1.1`, `4.0.1`, `4.1.0`, `4.2.0`, `4.3.0`, `4.4.0`, `5.0.0`, `5.1.0`, `5.2.0` |

Each release directory contains the original update blob, named `blob`. After extraction, the component files are written alongside it. For example:

```text
.
├── README.md
├── extract_shield_blob_components.py
├── sb-na-wf/
│   └── 0.0.0/
│       ├── blob
│       ├── BCT
│       ├── BMP
│       ├── EBT
│       ├── RBL
│       ├── DTB_01
│       ├── DTB_02
│       └── ...
├── wx-na-wf/
├── wx-na-do/
└── wx-un-do/
```

The tree above is illustrative; the exact component files depend on the release.

## Requirements

- Python 3.9 or newer
- No third-party Python packages are required

## Extracting components

Run the extractor against a release's `blob` file. Extracted components are written into the same directory as the input blob:

```bash
python3 extract_shield_blob_components.py sb-na-wf/0.0.0/blob
```

To list and validate the components without writing files:

```bash
python3 extract_shield_blob_components.py sb-na-wf/0.0.0/blob --list
```

## What the extractor does

The script parses the blob's top-level component table and writes each component as a separate file. It does not unpack or interpret the contents *inside* components such as the BCT or EBT; for example, `BCT` is extracted as a complete BCT binary.

It also:

- Determines the component payload offset adjustment automatically. In the SHIELD Tablet/K1 blobs, the table's stored offsets are `0x1C` (28) bytes before the actual payload starts.
- Validates the adjusted component ranges before writing files.
- Checks DTB payloads for the Flattened Device Tree magic and verifies that the embedded FDT size matches the component-table size.
- Checks the extracted BCT against basic Tegra124 BCT structure values.
- Preserves repeated component entries by numbering them in table order, such as `DTB_01`, `DTB_02`, and so on.
- Writes separately named components even when two table entries refer to the same payload. In these blobs, `EBT` and `RBL` can be aliases of the same bytes.

The script prints each component's table entry, stored and adjusted offsets, size, and a shortened SHA-256 digest. The full original blob is left unchanged.

An explicit offset adjustment can be supplied with `--offset-bias` if needed, for example `--offset-bias 0x1c`. The normal workflow should use automatic detection and validation.
