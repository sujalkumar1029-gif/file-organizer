#!/usr/bin/env python3
"""organize - sort files and folders into tidy category folders.

Safe by design:
  * dry run by default: nothing moves until you pass --apply
  * never overwrites an existing file
  * every applied run is journaled and can be reversed with `organize undo`
  * skips hidden files, symlinks, active downloads and project folders

Usage:  python3 organize.py --help
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

import fcntl

if sys.version_info < (3, 11):
    sys.exit("organize requires Python 3.11 or newer (for tomllib)")

APP = "file-organizer"
__version__ = "1.0.0"

# Folders we never descend into.
SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".cache", ".Trash", ".Trash-1000",
    "$RECYCLE.BIN", "System Volume Information", "lost+found",
}

# Markers that make a folder a "project".
PROJECT_MARKERS = (
    ".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "requirements.txt", "setup.py", "setup.cfg", "pom.xml", "build.gradle",
    "composer.json", "Gemfile", "CMakeLists.txt",
)

PARTIAL_EXTENSIONS = {"part", "crdownload", "download", "opdownload", "!ut", "tmp", "partial"}
COMPOUND_EXTENSIONS = ("tar.gz", "tar.bz2", "tar.xz", "tar.zst", "tar.lz4", "tar.z")
IMPLICIT_CATEGORIES = ("Duplicates", "Other", "Projects", "Backups", "Folders")

DEST_MARKER = "  ->  "


# --------------------------------------------------------------------------- paths

def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / APP


def config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("FILE_ORGANIZER_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / APP / "rules.toml"


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def is_forbidden_root(path: Path) -> bool:
    """Refuse $HOME, / and their ancestors: too much damage potential."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    home = Path.home().resolve()
    if resolved in (home, Path("/")):
        return True
    return is_inside(home, resolved)


def human_size(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def split_name(name: str) -> tuple[str, str]:
    """Split 'archive.tar.gz' into ('archive', '.tar.gz')."""
    low = name.lower()
    for ext in COMPOUND_EXTENSIONS:
        suffix = "." + ext
        if low.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], name[-len(suffix):]
    idx = name.rfind(".")
    if 0 < idx < len(name) - 1:
        return name[:idx], name[idx:]
    return name, ""


def matches_any(name: str, patterns: list[str] | tuple[str, ...]) -> bool:
    low = name.casefold()
    return any(fnmatch.fnmatch(low, p.casefold()) for p in patterns)


# --------------------------------------------------------------------------- colors

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if _COLOR else text


def dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if _COLOR else text


def yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m" if _COLOR else text


def red(text: str) -> str:
    return f"\033[31m{text}\033[0m" if _COLOR else text


class UserError(Exception):
    """A problem worth printing without a traceback."""


DEFAULT_CONFIG_TEXT = """\
# file-organizer rules.  Edit freely, then run `organize` to preview.
#
# Paths may start with ~.  Run `organize config --init` to install this file.

[general]
# Where organized files go. "" = organize in place (Category/ next to the file).
# Example: dest = "~/Organized"
dest = ""
# Folders used when no paths are given on the command line.
targets = ["~/Downloads", "~/Desktop", "~/Documents"]
# Also organize files inside subfolders (project folders are left intact).
recursive = false
# Move whole top-level folders into Projects/Backups/Folders (non-recursive only).
include_folders = false
# Put files in Category/YYYY/MM instead of just Category/.
date_folders = false
# Move exact duplicate files (same size + content) to Duplicates/.
dedupe = false
# Leave files modified in the last N minutes alone (downloads being written).
min_age_minutes = 2
# Unfinished downloads older than this many hours go to Incomplete/.
partial_stale_hours = 24
skip_hidden = true
# Ask `file(1)` about files whose extension is unknown.
sniff_unknown = true
exclude = ["desktop.ini", "Thumbs.db", "~$*"]

# Extensions are lowercase, without the dot. The first category wins.
[categories]
# Screenshots are recognised by name; see [patterns].
Screenshots = []
Images = ["jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "tif", "tiff", "heic", "heif", "ico", "svg", "jfif", "jxl", "raw", "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2"]
Videos = ["mp4", "mkv", "webm", "mov", "avi", "m4v", "wmv", "flv", "mpg", "mpeg", "3gp", "m2ts", "ogv"]
Audio = ["mp3", "wav", "flac", "ogg", "oga", "opus", "m4a", "aac", "wma", "aiff", "aif", "mid", "midi", "amr"]
PDFs = ["pdf"]
Documents = ["doc", "docx", "odt", "rtf", "txt", "md", "markdown", "tex", "epub", "mobi", "azw3", "pages", "rst", "org"]
Spreadsheets = ["xls", "xlsx", "xlsm", "ods", "csv", "tsv", "numbers"]
Presentations = ["ppt", "pptx", "pps", "ppsx", "odp", "key"]
Archives = ["zip", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "7z", "rar", "zst", "tzst", "lz", "lz4", "lzma", "cab"]
Programs = ["appimage", "deb", "rpm", "exe", "msi", "msix", "dmg", "pkg", "apk", "flatpak", "flatpakref", "snap", "run"]
Code = ["py", "pyw", "ipynb", "js", "mjs", "cjs", "ts", "tsx", "jsx", "html", "htm", "css", "scss", "sass", "less", "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg", "xml", "sh", "bash", "zsh", "fish", "ps1", "bat", "c", "h", "cpp", "hpp", "cc", "rs", "go", "java", "kt", "kts", "swift", "rb", "php", "pl", "lua", "sql", "r", "jl", "vue", "svelte", "dockerfile", "mk", "cmake", "gradle"]
Fonts = ["ttf", "otf", "ttc", "woff", "woff2", "eot", "dfont", "pfb"]
Design = ["psd", "psb", "ai", "eps", "fig", "sketch", "xd", "blend", "xcf", "kra", "afdesign", "afphoto", "procreate", "cdr"]
Models = ["pth", "pt", "ckpt", "onnx", "pkl", "joblib", "h5", "hdf5", "safetensors", "npz", "npy", "parquet", "feather", "arrow", "pb", "tflite", "gguf", "ggml", "model"]
DiskImages = ["iso", "img", "vhd", "vhdx", "vmdk", "qcow2"]
Incomplete = ["part", "crdownload", "download", "opdownload", "!ut", "tmp", "partial"]

# File-name globs (case-insensitive), checked before the extension table.
[patterns]
Screenshots = ["screenshot*", "screen shot*", "screencapture*", "cleanshot*", "snipaste*"]
"""


def _default_config() -> dict:
    import tomllib

    return tomllib.loads(DEFAULT_CONFIG_TEXT)


def load_config(path: Path, require: bool = False) -> dict:
    import tomllib

    cfg = _default_config()
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise UserError(f"cannot read config {path}: {exc}") from exc
        cfg["general"].update(data.get("general", {}))
        if "categories" in data:
            cfg["categories"] = data["categories"]
        if "patterns" in data:
            cfg["patterns"] = data["patterns"]
    elif require:
        raise UserError(f"config not found: {path}")

    for section in ("categories", "patterns"):
        for name, values in cfg.get(section, {}).items():
            if not isinstance(values, list):
                raise UserError(f"config error: [{section}] {name} must be a list")
    return cfg


class Rules:
    """Turns file names, extensions and content into a category."""

    def __init__(self, cfg: dict):
        self.ext_map: dict[str, str] = {}
        for category, extensions in cfg.get("categories", {}).items():
            for ext in extensions:
                key = str(ext).lower().strip().lstrip(".")
                if key:
                    self.ext_map.setdefault(key, category)
        self.patterns: list[tuple[str, str]] = []
        for category, patterns in cfg.get("patterns", {}).items():
            for pattern in patterns:
                self.patterns.append((str(pattern), category))
        self.category_names = {n.casefold() for n in cfg.get("categories", {})}
        self.category_names.update(n.casefold() for n in IMPLICIT_CATEGORIES)

    def classify_file(self, path: Path, settings: "Settings", st: os.stat_result) -> tuple[str | None, str]:
        name = path.name
        low = name.casefold()
        _, extension = split_name(name)
        ext = extension.lower().lstrip(".")
        age = max(0.0, time.time() - st.st_mtime)
        stale_after = settings.partial_stale_hours * 3600

        if ext in PARTIAL_EXTENSIONS:
            if age < stale_after:
                return None, "download in progress"
            return "Incomplete", "stale partial download"

        if st.st_size == 0:
            if age < stale_after:
                return None, "empty file (possibly in progress)"
            return "Incomplete", "empty file"

        if age < settings.min_age_minutes * 60:
            return None, "modified moments ago"

        for pattern, category in self.patterns:
            if fnmatch.fnmatch(low, pattern.casefold()):
                return category, "name pattern"

        if ext in self.ext_map:
            return self.ext_map[ext], f".{ext}"

        if settings.sniff_unknown:
            category = self.sniff(path)
            if category:
                return category, "content sniff"

        return "Other", "unrecognised type"

    @staticmethod
    def sniff(path: Path) -> str | None:
        mime, _ = mimetypes.guess_type(path.name)
        if not mime and shutil.which("file"):
            try:
                proc = subprocess.run(
                    ["file", "-b", "--mime-type", "--", str(path)],
                    capture_output=True, text=True, timeout=10,
                )
                mime = proc.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                mime = None
        if not mime or "/" not in mime:
            return None
        major = mime.split("/", 1)[0]
        if major in ("image", "video", "audio"):
            return {"image": "Images", "video": "Videos", "audio": "Audio"}[major]
        if mime == "application/pdf":
            return "PDFs"
        if major == "text":
            return "Documents"
        if mime in (
            "application/zip", "application/x-tar", "application/gzip",
            "application/x-7z-compressed", "application/vnd.rar",
            "application/x-xz", "application/zstd",
        ):
            return "Archives"
        return None


# --------------------------------------------------------------------------- model

@dataclass
class Settings:
    targets: list[Path]
    dest: Path | None = None
    recursive: bool = False
    include_folders: bool = False
    date_folders: bool = False
    dedupe: bool = False
    min_age_minutes: float = 2.0
    partial_stale_hours: float = 24.0
    skip_hidden: bool = True
    sniff_unknown: bool = True
    exclude: list[str] = field(default_factory=list)
    apply: bool = False
    verbose: bool = False
    json: bool = False
    quiet: bool = False


@dataclass
class Candidate:
    src: Path
    root: Path
    category: str
    reason: str
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0


@dataclass
class Move:
    src: Path
    dst: Path
    root: Path
    category: str
    reason: str
    is_dir: bool = False
    size: int = 0


@dataclass
class Skipped:
    path: Path
    reason: str


@dataclass
class RunResult:
    moved: list[Move] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    errors: list[tuple[Move, str]] = field(default_factory=list)
    journal: Path | None = None


def is_project_root(path: Path) -> bool:
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


def looks_like_backup(name: str) -> bool:
    low = name.casefold()
    return any(word in low for word in ("backup", "bkp", "bak", "copy of"))


# --------------------------------------------------------------------------- collect

def collect(settings: Settings, rules: Rules) -> tuple[list[Candidate], list[Skipped]]:
    candidates: list[Candidate] = []
    skipped: list[Skipped] = []

    for target in settings.targets:
        root = Path(target).expanduser()
        if is_forbidden_root(root):
            raise UserError(
                f"refusing to organize {root}: pass specific folders "
                "(Downloads, Desktop, ...) instead of your whole home directory"
            )
        if not root.is_dir():
            skipped.append(Skipped(root, "not a directory"))
            continue
        root = root.resolve()
        if settings.recursive and is_project_root(root):
            raise UserError(f"refusing to reorganize project folder {root} recursively")
        if settings.recursive:
            _walk_root(root, settings, rules, candidates, skipped)
        else:
            _scan_root(root, settings, rules, candidates, skipped)
    return candidates, skipped


def _scan_root(root, settings, rules, candidates, skipped) -> None:
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.casefold())
    except OSError as exc:
        skipped.append(Skipped(root, f"cannot read: {exc.strerror}"))
        return

    for entry in entries:
        if settings.dest and is_inside(entry, settings.dest):
            continue
        if settings.skip_hidden and entry.name.startswith("."):
            continue
        try:
            is_link = entry.is_symlink()
        except OSError:
            continue
        if is_link:
            skipped.append(Skipped(entry, "symlink (left alone)"))
            continue
        if entry.is_dir():
            if entry.name.casefold() in rules.category_names:
                continue
            if settings.include_folders:
                if is_project_root(entry):
                    category, reason = "Projects", "project folder"
                elif looks_like_backup(entry.name):
                    category, reason = "Backups", "looks like a backup"
                else:
                    category, reason = "Folders", "folder"
                candidates.append(Candidate(entry, root, category, reason, is_dir=True))
            continue
        if entry.is_file():
            _handle_file(entry, root, settings, rules, candidates, skipped)
        else:
            skipped.append(Skipped(entry, "not a regular file"))


def _walk_root(root, settings, rules, candidates, skipped) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        here = Path(dirpath)
        if here != root and is_project_root(here):
            dirnames[:] = []
            skipped.append(Skipped(here, "project folder (left intact)"))
            continue

        kept = []
        for name in sorted(dirnames):
            child = here / name
            if settings.skip_hidden and name.startswith("."):
                continue
            if name in SKIP_DIR_NAMES:
                continue
            if name.casefold() in rules.category_names:
                continue
            if matches_any(name, settings.exclude):
                continue
            if settings.dest and is_inside(child, settings.dest):
                continue
            if child.is_symlink():
                skipped.append(Skipped(child, "symlink (left alone)"))
                continue
            kept.append(name)
        dirnames[:] = kept

        for filename in sorted(filenames):
            path = here / filename
            if matches_any(filename, settings.exclude):
                skipped.append(Skipped(path, "excluded by pattern"))
                continue
            _handle_file(path, root, settings, rules, candidates, skipped)


def _handle_file(path, root, settings, rules, candidates, skipped) -> None:
    if path.is_symlink():
        skipped.append(Skipped(path, "symlink (left alone)"))
        return
    if settings.skip_hidden and path.name.startswith("."):
        return
    if matches_any(path.name, settings.exclude):
        skipped.append(Skipped(path, "excluded by pattern"))
        return
    try:
        st = path.stat()
    except OSError as exc:
        skipped.append(Skipped(path, f"cannot read: {exc.strerror}"))
        return
    category, reason = rules.classify_file(path, settings, st)
    if category is None:
        skipped.append(Skipped(path, reason))
        return
    candidates.append(
        Candidate(path, root, category, reason, False, st.st_size, st.st_mtime)
    )


# --------------------------------------------------------------------------- dedupe + plan

def _file_hash(path: Path) -> str | None:
    digest = hashlib.blake2b(digest_size=16)
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def mark_duplicates(candidates: list[Candidate]) -> None:
    by_size: dict[int, list[Candidate]] = {}
    for candidate in candidates:
        if candidate.is_dir or candidate.category == "Incomplete":
            continue
        by_size.setdefault(candidate.size, []).append(candidate)

    for items in by_size.values():
        if len(items) < 2:
            continue
        seen: dict[str, Candidate] = {}
        for candidate in items:
            digest = _file_hash(candidate.src)
            if digest is None:
                continue
            if digest in seen:
                candidate.category = "Duplicates"
                candidate.reason = f"exact duplicate of {seen[digest].src.name}"
            else:
                seen[digest] = candidate


def _unique_path(path: Path, reserved: set[Path]) -> Path:
    if path not in reserved and not path.exists():
        reserved.add(path)
        return path
    stem, extension = split_name(path.name)
    counter = 2
    while True:
        candidate = path.with_name(f"{stem} ({counter}){extension}")
        if candidate not in reserved and not candidate.exists():
            reserved.add(candidate)
            return candidate
        counter += 1


def plan_moves(candidates: list[Candidate], settings: Settings) -> list[Move]:
    reserved: set[Path] = set()
    moves: list[Move] = []
    for candidate in candidates:
        destination_root = settings.dest or candidate.root
        parts = [candidate.category]
        if settings.date_folders and not candidate.is_dir:
            stamp = time.localtime(candidate.mtime)
            parts.extend([f"{stamp.tm_year:04d}", f"{stamp.tm_mon:02d}"])
        target_dir = destination_root.joinpath(*parts)
        destination = _unique_path(target_dir / candidate.src.name, reserved)
        moves.append(
            Move(candidate.src, destination, candidate.root, candidate.category,
                 candidate.reason, candidate.is_dir, candidate.size)
        )
    return moves


# --------------------------------------------------------------------------- output

def _print_skipped(skipped: list[Skipped], settings: Settings) -> None:
    if not skipped:
        return
    counts = Counter(item.reason for item in skipped)
    summary = ", ".join(f"{count} {reason}" for reason, count in counts.most_common(3))
    print(yellow(f"Skipped {len(skipped)} item(s): {summary}"))
    if settings.verbose:
        for item in skipped:
            print(dim(f"  {item.path} - {item.reason}"))


def print_plan(moves: list[Move], skipped: list[Skipped], settings: Settings) -> None:
    if not moves:
        print("Nothing to organize - everything looks tidy." if not skipped else "Nothing to organize.")
        _print_skipped(skipped, settings)
        return

    if not settings.apply:
        print()
        print(f"{bold('Dry run')} - nothing will be moved.")

    by_root: dict[Path, list[Move]] = {}
    for move in moves:
        by_root.setdefault(move.root, []).append(move)

    total_bytes = sum(move.size for move in moves if not move.is_dir)
    for root, items in sorted(by_root.items(), key=lambda pair: str(pair[0])):
        where = "in place" if settings.dest is None else str(settings.dest)
        print(f"\n{bold(str(root))}  ->  {where}")

        by_category: dict[str, list[Move]] = {}
        for move in items:
            by_category.setdefault(move.category, []).append(move)

        for category in sorted(by_category):
            group = by_category[category]
            count = len(group)
            if group[0].is_dir:
                kind = "folder" if count == 1 else "folders"
                size_text = " " * 10
            else:
                kind = "file" if count == 1 else "files"
                size_text = f"{human_size(sum(m.size for m in group)):>10}"
            print(f"  {category:<16} {count:>3} {kind:<8}{size_text}")
            if settings.verbose:
                for move in group:
                    print(dim(f"      {move.src}  ->  {move.dst}"))

    print()
    print(f"{bold('Total')}: {len(moves)} item(s), {human_size(total_bytes)}")
    _print_skipped(skipped, settings)
    if not settings.apply:
        print(dim("Re-run with --apply to move these items."))


# --------------------------------------------------------------------------- apply

def ensure_dir(path: Path) -> list[Path]:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    created: list[Path] = []
    for directory in reversed(missing):
        try:
            directory.mkdir()
            created.append(directory)
        except FileExistsError:
            pass
    return created


def execute(moves: list[Move]) -> tuple[list[Move], list[tuple[Move, str]], list[Path]]:
    moved: list[Move] = []
    errors: list[tuple[Move, str]] = []
    created: list[Path] = []
    for move in moves:
        try:
            created.extend(ensure_dir(move.dst.parent))
            shutil.move(str(move.src), str(move.dst))
            moved.append(move)
        except (OSError, shutil.Error) as exc:
            errors.append((move, str(exc)))
    return moved, errors, created


def write_journal(moved: list[Move], created_dirs: list[Path]) -> Path | None:
    if not moved:
        return None
    runs = state_dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    payload = {
        "version": 1,
        "run_id": run_id,
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "moves": [
            {"src": str(m.src), "dst": str(m.dst), "category": m.category, "is_dir": m.is_dir}
            for m in moved
        ],
        "created_dirs": [str(d) for d in created_dirs],
    }
    path = runs / f"{run_id}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def print_result(moved: list[Move], errors: list[tuple[Move, str]], journal: Path | None) -> None:
    print(f"\n{bold('Done')} - moved {len(moved)} item(s).")
    for move, message in errors:
        print(f"  {red('failed')}: {move.src} -> {move.dst}: {message}")
    if journal:
        print(dim(f"Journal: {journal}"))
        print(f"Undo with: {bold('organize undo --last --apply')}")


@contextmanager
def run_lock() -> Iterator[None]:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / "lock", "w", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UserError(f"another organize run is in progress ({directory / 'lock'})") from exc
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


# --------------------------------------------------------------------------- undo

def list_runs() -> list[Path]:
    runs = state_dir() / "runs"
    if not runs.is_dir():
        return []
    return sorted(runs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def cmd_undo(args) -> int:
    runs = list_runs()
    if not runs:
        print("Nothing to undo - no recorded runs.")
        return 0

    if args.all:
        chosen = runs
    elif args.run:
        key = args.run.strip()
        chosen = [p for p in runs if p.stem == key or p.stem.startswith(key)]
        if not chosen:
            raise UserError(f"no journal found for run {key!r}")
    else:
        chosen = runs[:1]

    if not args.apply:
        print(f"{bold('Undo plan')} - nothing will be moved (dry run).")

    conflicts = 0
    restored = 0
    for journal_path in chosen:
        try:
            data = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(yellow(f"Skipping unreadable journal {journal_path}: {exc}"))
            conflicts += 1
            continue

        run_conflicts = 0
        run_restored = 0
        print(f"\n{bold(str(journal_path.stem))}  {data.get('time', '')}")
        for entry in reversed(data.get("moves", [])):
            source = Path(entry["src"])
            destination = Path(entry["dst"])
            if not (destination.exists() or destination.is_symlink()):
                print(dim(f"  missing: {destination}"))
                run_conflicts += 1
                continue
            if source.exists() or source.is_symlink():
                print(yellow(f"  exists, not overwritten: {source}"))
                run_conflicts += 1
                continue
            if args.apply:
                try:
                    ensure_dir(source.parent)
                    shutil.move(str(destination), str(source))
                except (OSError, shutil.Error) as exc:
                    print(red(f"  failed: {destination} -> {source}: {exc}"))
                    run_conflicts += 1
                    continue
            run_restored += 1
            if args.verbose:
                print(dim(f"  {destination}  ->  {source}"))

        restored += run_restored
        conflicts += run_conflicts
        print(f"  {'restored' if args.apply else 'would restore'} {run_restored} item(s), {run_conflicts} conflict(s)")

        if args.apply and run_conflicts == 0:
            for directory in sorted(data.get("created_dirs", []), key=len, reverse=True):
                try:
                    Path(directory).rmdir()
                except OSError:
                    pass
            archive = state_dir() / "runs" / "undone"
            archive.mkdir(parents=True, exist_ok=True)
            journal_path.rename(archive / journal_path.name)

    print(f"\n{bold('Total')}: {restored} item(s) {'restored' if args.apply else 'to restore'}, {conflicts} conflict(s)")
    if not args.apply and restored:
        print(dim("Re-run with --apply to undo."))
    return 1 if (args.apply and conflicts) else 0


# --------------------------------------------------------------------------- run

def _pick(cli_value, config_value):
    return config_value if cli_value is None else cli_value


def settings_from_args(args, cfg: dict) -> Settings:
    general = cfg.get("general", {})
    raw_targets = list(getattr(args, "paths", None) or general.get("targets") or ["~/Downloads"])
    targets = [Path(str(item)).expanduser() for item in raw_targets]

    dest_value = general.get("dest", "") if args.dest is None else args.dest
    dest = Path(str(dest_value)).expanduser().resolve() if dest_value else None

    recursive = bool(_pick(args.recursive, general.get("recursive", False)))
    include_folders = bool(_pick(args.include_folders, general.get("include_folders", False)))
    if recursive and include_folders:
        raise UserError("--include-folders cannot be combined with --recursive; run them separately")

    exclude = list(general.get("exclude") or [])
    exclude.extend(args.exclude or [])

    return Settings(
        targets=targets,
        dest=dest,
        recursive=recursive,
        include_folders=include_folders,
        date_folders=bool(_pick(args.date_folders, general.get("date_folders", False))),
        dedupe=bool(_pick(args.dedupe, general.get("dedupe", False))),
        min_age_minutes=float(_pick(args.min_age, general.get("min_age_minutes", 2))),
        partial_stale_hours=float(general.get("partial_stale_hours", 24)),
        skip_hidden=bool(general.get("skip_hidden", True)),
        sniff_unknown=bool(_pick(args.sniff, general.get("sniff_unknown", True))),
        exclude=exclude,
        apply=bool(getattr(args, "apply", False)),
        verbose=bool(getattr(args, "verbose", False)),
        json=bool(getattr(args, "json", False)),
    )


def _organize(settings: Settings, rules: Rules) -> RunResult:
    candidates, skipped = collect(settings, rules)
    if settings.dedupe:
        mark_duplicates(candidates)
    moves = plan_moves(candidates, settings)

    payload = {
        "apply": settings.apply,
        "moves": [
            {
                "src": str(move.src), "dst": str(move.dst), "category": move.category,
                "reason": move.reason, "is_dir": move.is_dir, "size": move.size,
            }
            for move in moves
        ],
        "skipped": [{"path": str(item.path), "reason": item.reason} for item in skipped],
        "summary": {
            "items": len(moves),
            "bytes": sum(move.size for move in moves if not move.is_dir),
        },
    }

    result = RunResult(skipped=skipped)
    if not settings.json and not settings.quiet:
        print_plan(moves, skipped, settings)

    if settings.apply and moves:
        moved, errors, created = execute(moves)
        result.moved = moved
        result.errors = errors
        result.journal = write_journal(moved, created)
        if settings.json:
            payload["moved"] = [{"src": str(m.src), "dst": str(m.dst)} for m in moved]
            payload["errors"] = [{"src": str(m.src), "error": message} for m, message in errors]
            payload["journal"] = str(result.journal) if result.journal else None
        elif not settings.quiet:
            print_result(moved, errors, result.journal)

    if settings.json:
        print(json.dumps(payload, indent=2))
    return result


def run_organize(settings: Settings, rules: Rules) -> RunResult:
    if settings.apply:
        with run_lock():
            return _organize(settings, rules)
    return _organize(settings, rules)


def cmd_watch(args, settings: Settings, rules: Rules) -> int:
    settings.apply = True
    settings.quiet = True
    interval = max(1.0, float(args.interval))
    print(f"Watching {', '.join(str(t) for t in settings.targets)} every {interval:g}s. Ctrl-C to stop.")
    try:
        while True:
            started = time.time()
            try:
                result = run_organize(settings, rules)
                if result.moved or result.errors:
                    extra = f", {len(result.errors)} error(s)" if result.errors else ""
                    print(f"[{time.strftime('%H:%M:%S')}] moved {len(result.moved)} item(s){extra}")
            except UserError as exc:
                print(red(f"error: {exc}"))
                return 2
            except Exception as exc:  # keep watching through transient errors
                print(red(f"error: {exc}"))
            time.sleep(max(1.0, interval - (time.time() - started)))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# --------------------------------------------------------------------------- timer / config

def parse_interval(text: str) -> int:
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)?\s*",
        str(text).casefold(),
    )
    if not match:
        raise UserError(f"invalid interval {text!r} - try 900, 30min or 1h")
    value = float(match.group(1))
    unit = (match.group(2) or "s")[0]
    factor = {"d": 86400, "h": 3600, "m": 60}.get(unit, 1)
    return max(1, int(value * factor))


def cmd_timer(args) -> int:
    seconds = parse_interval(args.interval)
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    command = shlex.join([sys.executable, str(script), "run", "--apply", "--config",
                          str(config_path())])

    service_path = unit_dir / "file-organizer.service"
    timer_path = unit_dir / "file-organizer.timer"
    service_path.write_text(
        "[Unit]\nDescription=Organize files into category folders\n\n"
        f"[Service]\nType=oneshot\nExecStart={command}\n",
        encoding="utf-8",
    )
    timer_path.write_text(
        "[Unit]\nDescription=Run file-organizer periodically\n\n"
        f"[Timer]\nOnBootSec=5min\nOnUnitActiveSec={seconds}s\n"
        "AccuracySec=1min\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    print(f"Wrote {service_path}")
    print(f"Wrote {timer_path}")

    if args.enable:
        if not shutil.which("systemctl"):
            raise UserError("systemctl not found - enable the timer manually")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        proc = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "file-organizer.timer"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(red(proc.stderr.strip() or proc.stdout.strip()))
            return 1
        print("Timer enabled. Check it with: systemctl --user list-timers file-organizer.timer")
    else:
        print("Enable it with: systemctl --user enable --now file-organizer.timer")
    return 0


def cmd_config(args) -> int:
    path = config_path(args.config)
    if args.init:
        if path.exists() and not args.force:
            print(f"Config already exists: {path}\nUse --force to overwrite it.")
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
        print(f"Wrote {path}")
        return 0
    print(f"Config: {path}")
    if path.exists():
        cfg = load_config(path)
        print(f"Status: found ({len(cfg['categories'])} categories, {len(cfg['patterns'])} pattern groups)")
    else:
        print("Status: not created yet - built-in rules are in use")
        print(f"Create it with: organize config --init")
    return 0


# --------------------------------------------------------------------------- CLI

def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="folders to organize (default: [general] targets)")
    parser.add_argument("--apply", action="store_true",
                        help="actually move items (default is a dry run)")
    parser.add_argument("--dest", metavar="DIR",
                        help='collect everything under DIR; "" means organize in place')
    parser.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=None,
                        help="also organize files inside subfolders")
    parser.add_argument("--include-folders", "--folders", dest="include_folders",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="move whole top-level folders into Projects/Backups/Folders")
    parser.add_argument("--date-folders", action=argparse.BooleanOptionalAction, default=None,
                        help="sort files into Category/YYYY/MM")
    parser.add_argument("--dedupe", action=argparse.BooleanOptionalAction, default=None,
                        help="move exact duplicates to Duplicates/")
    parser.add_argument("--sniff", dest="sniff", action=argparse.BooleanOptionalAction, default=None,
                        help="inspect unknown files with file(1)")
    parser.add_argument("--min-age", type=float, metavar="MINUTES", default=None,
                        help="leave files modified in the last N minutes alone")
    parser.add_argument("--exclude", action="append", metavar="GLOB", default=None,
                        help="extra file-name glob to ignore (repeatable)")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every planned move")
    parser.add_argument("--json", action="store_true", help="machine-readable plan on stdout")
    parser.add_argument("--config", metavar="FILE", default=None, help="rules file to use")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="organize",
        description="Sort files and folders into category folders. Dry run by default; "
                    "nothing moves without --apply.",
        epilog="Examples:\n"
               "  organize                          preview ~/Downloads, ~/Desktop, ~/Documents\n"
               "  organize --apply                  move them\n"
               "  organize ~/Downloads --apply -v   organize one folder, show every move\n"
               "  organize undo --last --apply      put the last run back\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="classify items (dry run unless --apply)")
    add_run_arguments(run_parser)

    undo_parser = sub.add_parser("undo", help="reverse an applied run")
    undo_parser.add_argument("--run", metavar="ID", help="journal id or prefix (default: latest)")
    undo_parser.add_argument("--all", action="store_true", help="undo every recorded run")
    undo_parser.add_argument("--apply", action="store_true", help="actually restore")
    undo_parser.add_argument("-v", "--verbose", action="store_true")

    watch_parser = sub.add_parser("watch", help="keep organizing as new files appear")
    add_run_arguments(watch_parser)
    watch_parser.add_argument("--interval", default="60", help="seconds between passes (default 60)")

    config_parser = sub.add_parser("config", help="show or create the rules file")
    config_parser.add_argument("--init", action="store_true", help="write the default rules file")
    config_parser.add_argument("--force", action="store_true", help="overwrite existing rules")
    config_parser.add_argument("--config", metavar="FILE", default=None)

    timer_parser = sub.add_parser("timer", help="install a systemd user timer")
    timer_parser.add_argument("--interval", default="30min", help="how often to run (default 30min)")
    timer_parser.add_argument("--enable", action="store_true", help="enable and start it now")
    return parser


COMMANDS = ("run", "undo", "watch", "config", "timer")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["run"]
    elif argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "run")

    args = build_parser().parse_args(argv)
    try:
        if args.command in ("run", "watch"):
            cfg = load_config(config_path(args.config))
            settings = settings_from_args(args, cfg)
            rules = Rules(cfg)
            if args.command == "watch":
                return cmd_watch(args, settings, rules)
            result = run_organize(settings, rules)
            return 1 if result.errors else 0
        if args.command == "undo":
            return cmd_undo(args)
        if args.command == "config":
            return cmd_config(args)
        if args.command == "timer":
            return cmd_timer(args)
    except UserError as exc:
        print(red(f"error: {exc}"), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())




