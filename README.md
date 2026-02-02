# bioinfo_archivist

Zero-dependency Python script to archive old bioinformatics analysis run folders.

Usage
```
python bioinfo_archivist.py <input_folder> [output_folder]
```

Examples
- Dry-run (preview):
```
python bioinfo_archivist.py /path/to/runs --dry-run
```

- Archive and move to custom output:
```
python bioinfo_archivist.py /path/to/runs /data/archives
```

Environment
- You can set `BIOINFO_ARCHIVIST_OUTPUT` to provide a default output folder.

Behavior
- Scans immediate subfolders of `<input_folder>` older than 6 months (default).
- Uses a `find | tar` pipeline to select many common log/source file extensions and compress them into a `.tar.gz`.
- Moves archives to the destination folder and deletes the original folders.
- Appends a tab-separated log entry to `archive.log` in the destination folder with fields:
  `ISO8601_timestamp<TAB>hostname<TAB>original_path<TAB>size_bytes`

Safety
- Use `--dry-run` to preview commands before executing.
- Use `--no-log` to disable logging.

Notes
- Requires `find` and `tar` available in PATH. On some systems (BSD tar) the `--null -T -` options may behave differently; test with `--dry-run` first.
