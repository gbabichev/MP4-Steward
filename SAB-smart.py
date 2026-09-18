#!/usr/bin/env python3

"""SABnzbd entry point for automatic remux-or-encode processing.

Usage:
    Configure SABnzbd to run this file as a post-processing script. SAB passes
    the completed download folder automatically; for a manual test, run:

        python3 SAB-smart.py "/path/to/completed/download"

Smart mode remuxes compatible files at or below 25 MB/min and uses H.265 for
larger or incompatible files. Set MP4_SMART_TARGET_MB_PER_MINUTE to customize
the threshold. See readme.md beside this script for complete setup and behavior.
"""

import os
import sys

import MP4_Steward


def main(arguments: list[str]) -> int:
    if len(arguments) < 2 or not arguments[1].strip():
        print("Usage: SAB-smart.py <SAB download folder>", file=sys.stderr)
        return 2

    download_folder = os.path.abspath(arguments[1])
    success = MP4_Steward.process_folder(
        input_path=download_folder,
        output_path=download_folder,
        first_run="smart",
        create_subfolders=False,
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
