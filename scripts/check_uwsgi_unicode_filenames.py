# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""Check that the uWSGI interpreter can extract non-ASCII file names.

The Python interpreter embedded in uWSGI selects its filesystem encoding from
the process locale at startup, which may differ from a standalone ``python3``
process in the same container. This script is therefore meant to be run inside
the container image by uWSGI itself::

    uwsgi --pyrun scripts/check_uwsgi_unicode_filenames.py
"""

import os
import sys
import tempfile
import zipfile

from reana_server.fetcher import ParsedUrl, WorkflowFetcherZip

FILE_NAMES = [
    "reana.yaml",
    ".github/ISSUE_TEMPLATE/✨-feature-request.md",
    ".github/ISSUE_TEMPLATE/\U0001fab2-bug-report.md",
    "docs/résumé/日本語.txt",
]


def main():
    """Extract an archive with non-ASCII file names and verify the result."""
    encoding = sys.getfilesystemencoding()
    print(f"uWSGI filesystem encoding: {encoding}")
    if encoding.lower() != "utf-8":
        print("ERROR: uWSGI filesystem encoding is not UTF-8")
        return 1
    with tempfile.TemporaryDirectory() as tmp_dir:
        archive_path = os.path.join(tmp_dir, "archive.zip")
        output_dir = os.path.join(tmp_dir, "output")
        os.mkdir(output_dir)
        with zipfile.ZipFile(archive_path, "w") as archive:
            for file_name in FILE_NAMES:
                archive.writestr(file_name, "workflow: {}\n")
        fetcher = WorkflowFetcherZip(ParsedUrl("file:///archive.zip"), output_dir)
        fetcher.extract_archive(archive_path)
        missing = [
            file_name
            for file_name in FILE_NAMES
            if not os.path.isfile(os.path.join(output_dir, *file_name.split("/")))
        ]
    if missing:
        print(f"ERROR: file names not preserved: {[ascii(m) for m in missing]}")
        return 1
    print("OK: non-ASCII file names extracted successfully")
    return 0


if __name__ == "__main__":
    # ``os._exit`` makes uWSGI terminate with the status of this check.
    sys.stdout.flush()
    status = 1
    try:
        status = main()
    finally:
        sys.stdout.flush()
        os._exit(status)
