"""5 GB of disk kept free for the system (system packages, if the situation forces).

Every download command (--main, --download, --process, --scribd, --phatmal)
checks it before each file. Under it the crawler quits hard: downloading
stops and the run exits with an error; main.py mails the failure right away
(the owner: "5gb is crawler hard quit for all flags"). "Tight" means exactly
that: under 5 GB free.
"""

from __future__ import annotations

import shutil
from pathlib import Path

DISK_FLOOR = 5 * 1024**3  # bytes kept free for the system; under it = tight = hard quit


def size_text(count: float) -> str:
    if count >= 1024**3:
        return f"{count / 1024**3:.1f} GB"
    return f"{count / 1048576:.1f} MB"


def free_bytes(path: str | Path) -> int:
    """Free bytes on the disk holding path (or its nearest existing parent)."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def disk_tight(path: str | Path) -> tuple[bool, int]:
    """(tight, free bytes): tight = under the DISK_FLOOR kept for the system."""
    free = free_bytes(path)
    return free < DISK_FLOOR, free


class DiskTight(BaseException):
    """The disk went under DISK_FLOOR before a download.

    A BaseException (like Ctrl+C), so no ``except Exception`` in a crawler
    swallows it; main.py turns it into an error exit + failure email.
    ``summary`` is the crawler's DownloadSummary when Dcrawler raised it.
    """

    def __init__(self, free: int) -> None:
        self.free = free
        self.summary = None
        super().__init__(
            f"only {size_text(free)} free, under the {size_text(DISK_FLOOR)} kept for "
            "the system: downloading stopped"
        )


def check_disk(path: str | Path) -> None:
    """Before each download: raise DiskTight when the disk is under DISK_FLOOR."""
    tight, free = disk_tight(path)
    if tight:
        raise DiskTight(free)
