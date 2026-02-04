#!/usr/bin/env python3
"""
bioinfo_archivist.py

Zero-dependency archivist for bioinformatics analysis runs.

Usage:
  python bioinfo_archivist.py <input_folder> [output_folder]

Defaults:
  - threshold: 6 months
  - output folder: env BIOINFO_ARCHIVIST_OUTPUT or ./archives

This script scans immediate subdirectories of <input_folder> for modification
time older than the threshold, shows them to the user, and on confirmation
archives selected files using a `find | tar` pipeline similar to the
provided `compress_logs` function. Archives are moved to the destination
folder and original folders are not deleted automatically (the script prints
`rm -rf` commands for the user to run). Each successful archive is logged
to `archive.log` in the destination folder.

Notes:
    - Requires GNU findutils (`find` on Linux, or `gfind` from Homebrew findutils on macOS) and `tar` available in PATH.
  - Designed to be zero-install (standard library only).
"""

from __future__ import annotations

import argparse
import datetime
import errno
import fcntl
import os
import shutil
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
from typing import List, Tuple


# Regex for GNU find's `-iregex` when used with `-regextype posix-extended`.
EXT_REGEX = (
    r".*\.(err|out|stdOut|stdErr|pdf|yaml|yml|xml|json|md|settings|txt|log|html|tsv|csv|"
    r"slurm|sbatch|sh|py|R|conf|config|ini)$"
)


def require_gnu_find() -> str:
    """Return a GNU findutils binary path.

    On macOS, Homebrew findutils installs GNU find as `gfind`.
    """
    for prog in ("gfind", "find"):
        path = shutil.which(prog)
        if not path:
            continue
        try:
            res = subprocess.run([path, "--version"], check=False, capture_output=True, text=True)
        except Exception:
            continue
        if res.returncode == 0 and "GNU findutils" in (res.stdout or ""):
            return path
    raise RuntimeError(
        "GNU findutils is required. On macOS: `brew install findutils` and ensure `gfind` is in PATH."
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Archive old bioinformatics run folders into tar.gz archives and log activity."
    )
    p.add_argument("input_folder", help="Folder containing run subfolders to scan")
    p.add_argument("output_folder", nargs="?", help="Destination folder for archives (overrides env)")
    p.add_argument("--months", type=int, default=6, help="Age threshold in months (default: 6)")
    p.add_argument("--dry-run", action="store_true", help="Show actions without executing them")
    p.add_argument("--verbose", "-v", action="store_true", help="Print the find | tar commands the script runs")
    p.add_argument("--use-lock", action="store_true", help="Use advisory lock when appending to log")
    p.add_argument("--log-rotate-size", type=int, default=0, help="Rotate log if larger than bytes (0=disabled)")
    p.add_argument("--keep", type=int, default=5, help="Number of rotated logs to keep")
    return p.parse_args()


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def human_readable(n: int) -> str:
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if abs(n) < 1024.0:
            return f"{n:3.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PiB"


def get_folder_size(path: str) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                st = os.stat(fp, follow_symlinks=True)
            except OSError:
                continue
            # Only regular files contribute size
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def find_old_subdirs(input_folder: str, months: int) -> List[Tuple[str, float]]:
    cutoff = datetime.datetime.now().timestamp() - months * 30 * 24 * 3600
    results = []
    with os.scandir(input_folder) as it:
        for entry in it:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            try:
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                results.append((entry.path, mtime))
    return sorted(results, key=lambda x: x[1])


def archive_folder(
    folder: str,
    dest_folder: str,
    dry_run: bool = False,
    verbose: bool = False,
    source_dir: str | None = None,
    find_bin: str | None = None,
) -> Tuple[str, int]:
    trim_path = folder.rstrip("/\\")
    base = os.path.basename(trim_path)
    if not base:
        base = "archive"

    # Prefer to run from the overall source directory that contains project
    # subfolders (i.e., the script input folder). Fall back to the folder's
    # parent if the computed relative path escapes the source dir.
    source_root = source_dir or (os.path.dirname(trim_path) or os.curdir)
    rel_project = os.path.relpath(trim_path, start=source_root)
    if rel_project == os.pardir or rel_project.startswith(os.pardir + os.sep):
        source_root = os.path.dirname(trim_path) or os.curdir
        rel_project = base

    os.makedirs(dest_folder, exist_ok=True)

    # Create archive in a temporary file then move to destination
    fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
    os.close(fd)

    # Run from the source root so file list paths match what tar expects,
    # and keep the top-level project folder name inside the archive.
    find_prog = find_bin or "find"
    find_cmd = (
        "cd "
        + shlex.quote(source_root)
        + " && "
        + shlex.quote(find_prog)
        + shlex.quote(rel_project)
        + " -regextype posix-extended -type f -iregex '"
        + EXT_REGEX
        + "' -size -10M -print0 | tar -czf "
        + shlex.quote(tmp_path)
        + " --null -T -"
    )

    if dry_run:
        print(f"DRY RUN: would run: {find_cmd}")
        return (tmp_path, 0)

    if verbose:
        print(f"VERBOSE: running: {find_cmd}")

    try:
        # Run via bash to preserve regex quoting behaviour
        subprocess.run(find_cmd, shell=True, check=True, executable="/bin/bash")
    except subprocess.CalledProcessError as e:
        # Clean temp
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise RuntimeError(f"Archiving failed for {folder}: {e}")

    archive_name = f"{base}.tar.gz"
    final_path = os.path.join(dest_folder, archive_name)
    # If name exists, add a timestamp
    if os.path.exists(final_path):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        final_path = os.path.join(dest_folder, f"{base}.{stamp}.tar.gz")

    shutil.move(tmp_path, final_path)
    size = os.path.getsize(final_path)
    return (final_path, size)


def atomic_append_log(logpath: str, line: str, use_lock: bool = False, rotate_size: int = 0, keep: int = 5) -> None:
    # Ensure folder exists
    logdir = os.path.dirname(logpath)
    if logdir:
        os.makedirs(logdir, exist_ok=True)

    # Rotation if requested
    try:
        if rotate_size > 0 and os.path.exists(logpath) and os.path.getsize(logpath) >= rotate_size:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            rotated = f"{logpath}.{ts}"
            os.rename(logpath, rotated)
            # Optionally compress rotated file (keeps zero-deps)
            try:
                import gzip

                with open(rotated, "rb") as f_in, gzip.open(rotated + ".gz", "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                os.unlink(rotated)
                rotated = rotated + ".gz"
            except Exception:
                pass
            # Prune old rotated files
            base = os.path.basename(logpath)
            files = [
                os.path.join(logdir, fn)
                for fn in sorted(os.listdir(logdir), reverse=True)
                if fn.startswith(base + ".")
            ]
            for old in files[keep:]:
                try:
                    os.unlink(old)
                except Exception:
                    pass
    except Exception:
        # If rotation fails, continue to attempt to write
        pass

    data = (line + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    mode = 0o644
    fd = None
    try:
        fd = os.open(logpath, flags, mode)
        if use_lock:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except Exception:
                pass
        os.write(fd, data)
        try:
            os.fsync(fd)
        except Exception:
            pass
    finally:
        if fd is not None:
            try:
                if use_lock:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                os.close(fd)
            except Exception:
                pass


def write_log_entry(dest_folder: str, folder_path: str, original_size: int, use_lock: bool, rotate_size: int, keep: int) -> None:
    hostname = socket.gethostname()
    ts = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    logline = "\t".join([ts, hostname, folder_path, str(original_size)])
    logpath = os.path.join(dest_folder, "archive.log")
    atomic_append_log(logpath, logline, use_lock=use_lock, rotate_size=rotate_size, keep=keep)


def confirm(prompt: str) -> bool:
    try:
        r = input(prompt + " ")
    except EOFError:
        return False
    return r.strip().lower() in ("y", "yes")


def main() -> int:
    args = parse_args()
    input_folder = args.input_folder
    output_folder = args.output_folder or os.environ.get("BIOINFO_ARCHIVIST_OUTPUT") or os.path.join(os.getcwd(), "archives")

    try:
        find_bin = require_gnu_find()
    except Exception as e:
        print(f"Error: {e}")
        return 2

    if not os.path.isdir(input_folder):
        print(f"Error: input folder does not exist or is not a directory: {input_folder}")
        return 2

    old_dirs = find_old_subdirs(input_folder, args.months)
    if not old_dirs:
        print("No subfolders older than threshold found.")
        return 0

    print("Folders to archive:")
    folders = []
    total_size = 0
    for path, mtime in old_dirs:
        size = get_folder_size(path)
        folders.append((path, size))
        total_size += size
        print(f" - {path}  ({human_readable(size)})")

    print(f"\nTotal size to archive: {human_readable(total_size)}")
    if not confirm("Proceed with archiving? (Will print rm -rf commands for manual deletion) [y/N]"):
        print("Aborted by user.")
        return 0

    archived_total = 0
    archive_sizes = 0
    for path, orig_size in folders:
        print(f"Archiving: {path} ...")
        try:
            archive_path, arc_size = archive_folder(
                path,
                output_folder,
                dry_run=args.dry_run,
                verbose=args.verbose,
                source_dir=input_folder,
                find_bin=find_bin,
            )
        except Exception as e:
            print(f"Failed to archive {path}: {e}")
            continue

        print(f" -> archive: {archive_path} ({human_readable(arc_size)})")
        archive_sizes += arc_size

        # Instead of automatically deleting folders, print a shell command
        # the user can run manually. Preserve dry-run messaging.
        rm_cmd = f"rm -rf {shlex.quote(path)}"
        if args.dry_run:
            print(f"DRY RUN: would run: {rm_cmd}")
        else:
            print(rm_cmd)

        if not args.dry_run:
            try:
                write_log_entry(output_folder, path, orig_size, args.use_lock, args.log_rotate_size, args.keep)
            except Exception as e:
                print(f"Warning: failed to write log entry: {e}")

        archived_total += orig_size

    reclaimed = archived_total - archive_sizes
    print("\nSummary:")
    print(f"  Original total size: {human_readable(archived_total)}")
    print(f"  Archives total size: {human_readable(archive_sizes)}")
    print(f"  Approx reclaimed space: {human_readable(reclaimed)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
