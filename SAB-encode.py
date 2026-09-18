#!/usr/bin/env python3

"""SABnzbd entry point for unattended H.265 encoding.

Usage:
    Configure SABnzbd to run this file as a post-processing script. SAB passes
    the completed download folder automatically; for a manual test, run:

        python3 SAB-encode.py "/path/to/completed/download"

See readme.md beside this script for complete setup, behavior, and requirements.
"""

import os
import sys

import MP4_Steward


def main(arguments: list[str]) -> int:
    if len(arguments) < 2 or not arguments[1].strip():
        print("Usage: SAB-encode.py <SAB download folder>", file=sys.stderr)
        return 2

    download_folder = os.path.abspath(arguments[1])
    success = MP4_Steward.process_folder(
        input_path=download_folder,
        output_path=download_folder,
        first_run="encode",
        create_subfolders=False,
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
