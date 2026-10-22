# file-organizer

**Sort loose files, images, screenshots and folders into tidy category folders — safely, from the terminal.**

`organize` is a single-file Python CLI for Linux. Point it at `~/Downloads`, `~/Desktop` or
`~/Documents` and it moves new items into `Images/`, `PDFs/`, `Screenshots/`, `Archives/`
and friends. Dry-run by default, never overwrites, and every applied run can be undone.

```console
$ organize
Dry run - nothing will be moved.

/home/you/Downloads  ->  in place
  Archives           2 files      12.4 MB
  Images            14 files      26.0 MB
  PDFs               3 files       8.8 MB
  Screenshots        5 files       1.2 MB
  Videos             1 file      290.8 KB

Total: 25 item(s), 48.6 MB
Re-run with --apply to move these items.
```

- [Features](#features)
- [Safety model](#safety-model)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Categories](#categories)
- [Configuration](#configuration)
- [Automation](#automation)
- [Command reference](#command-reference)
- [Undo and journals](#undo-and-journals)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Development](#development)
- [License](#license)

## Features

- **Dry run by default** — preview every move before anything happens.
- **Never overwrites** — name clashes become `photo (2).png`.
- **Undo** — `organize undo --last --apply` restores the previous run.
- **Screenshots** are detected by filename, not just extension.
- **Downloads-aware** — `.part` / `.crdownload` files and files modified in the last
  10 minutes are left alone.
- **Folder-aware** — optionally sort whole folders into `Projects/`, `Backups/` and `Folders/`.
- **Project-safe** — never reorganises inside `.git`, `node_modules`, virtualenvs or project roots.
- **Duplicate detection** (opt-in) — exact copies go to `Duplicates/`.
- **Date folders** (opt-in) — `Images/2026/10/photo.png`.
- **Automation** — systemd user timer, or a built-in `watch` mode.
- **No dependencies** — Python standard library only.
- **No network access** — everything stays on your machine.

## Safety model

- Nothing moves without `--apply`.
- Existing files are never replaced.
- Hidden files, symlinks and non-regular files are skipped.
- Project folders (`.git`, `package.json`, `pyproject.toml`, `Cargo.toml`, ...) are left intact.
- Active downloads (`.part`, `.crdownload`, `.download`, `.tmp`) are skipped until 24h old.
- Files modified in the last 10 minutes are skipped by default.
- `$HOME` and `/` are refused as targets — pass specific folders.
- A lock file prevents two runs from colliding.
- Every applied run is journaled in `~/.local/state/file-organizer/runs/`.

## Requirements

- Linux (uses `fcntl`; systemd is optional)
- Python **3.11+**
- `file(1)` — optional, improves detection of files without an extension

## Installation

### Option A — clone and link (recommended)

```bash
git clone https://github.com/sujalkumar1029-gif/file-organizer.git
cd file-organizer
mkdir -p ~/.local/bin
ln -sf "$PWD/organize.py" ~/.local/bin/organize
```

Make sure `~/.local/bin` is on your `PATH`, then check:

```bash
organize --version
```

### Option B — pipx

```bash
git clone https://github.com/sujalkumar1029-gif/file-organizer.git
pipx install ./file-organizer
```

### First run

```bash
organize config --init   # optional: write ~/.config/file-organizer/rules.toml
organize                 # dry run using the default targets
```

The default targets are `~/Downloads`, `~/Desktop` and `~/Documents`.

## Quick start

```bash
organize                          # preview
organize --apply                  # perform the moves
organize ~/Downloads --apply -v   # one folder, show every move
organize undo --last --apply      # put everything back
```

Every command accepts specific paths; with none it uses the configured targets.

## How it works

```
systemd timer or watch loop (optional)
        │
        ▼
organize.py
  1. lock        one run at a time
  2. read rules  ~/.config/file-organizer/rules.toml (or built-in defaults)
  3. scan        top level of each target (subfolders with --recursive)
  4. skip        hidden files, symlinks, projects, active downloads, fresh files
  5. classify    name pattern -> extension table -> file(1) -> Other
  6. plan        <folder>/<Category>/<name>; clashes get " (2)"
  7. move        shutil.move (an instant rename on the same filesystem)
  8. journal     record src/dst pairs for undo
```

Classification order matters:

1. **Name patterns** from `[patterns]` (e.g. `Screenshot*`).
2. **Extension table** from `[categories]` (first category listing an extension wins).
3. **Content sniffing** with `file(1)` for unknown extensions.
4. **Other** if nothing matches.

## Categories

| Category | Examples |
| --- | --- |
| `Screenshots` | `Screenshot From 2026-10-22.png`, `CleanShot 2026-...png` |
| `Images` | jpg, png, gif, webp, avif, svg, heic, raw, psd-adjacent formats |
| `Videos` | mp4, mkv, webm, mov, avi |
| `Audio` | mp3, wav, flac, ogg, opus, m4a |
| `PDFs` | pdf |
| `Documents` | docx, odt, txt, md, epub, tex |
| `Spreadsheets` | xlsx, ods, csv, tsv |
| `Presentations` | pptx, odp, key |
| `Archives` | zip, tar.gz, 7z, rar, xz |
| `Programs` | AppImage, deb, rpm, exe, flatpakref |
| `Code` | py, js, ts, html, json, yaml, sh, ipynb |
| `Fonts` | ttf, otf, woff2 |
| `Design` | psd, ai, fig, sketch, xcf |
| `Models` | pth, onnx, safetensors, pkl, gguf |
| `DiskImages` | iso, img, vmdk |
| `Incomplete` | stale `.part` / `.crdownload` and old empty files |
| `Other` | anything unrecognised |

Optional categories: `Duplicates` (with `--dedupe`), `Projects` / `Backups` / `Folders`
(with `--include-folders`).

## Configuration

```bash
organize config           # show the path and status
organize config --init    # create it
organize config --init --force   # reset to defaults
```

Edit `~/.config/file-organizer/rules.toml`:

```toml
[general]
# "" = organize in place; or collect everything under one tree:
dest = ""
targets = ["~/Downloads", "~/Desktop", "~/Documents"]
recursive = false          # also sort files in subfolders
include_folders = false    # move whole folders to Projects/Backups/Folders
date_folders = false       # Category/YYYY/MM
dedupe = false             # move exact duplicates to Duplicates/
min_age_minutes = 10       # ignore files touched this recently
partial_stale_hours = 24   # age before a .part file goes to Incomplete/
skip_hidden = true
sniff_unknown = true       # use file(1) on unknown types
exclude = ["desktop.ini", "Thumbs.db", "~$*"]

[categories]
# lowercase, no leading dot; first category listing an extension wins
Screenshots = []
Images = ["jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "heic"]
PDFs = ["pdf"]
Documents = ["doc", "docx", "odt", "txt", "md"]
Invoices = ["invoice", "receipt"]      # add your own categories

[patterns]
# globs on the whole filename, case-insensitive
Screenshots = ["screenshot*", "screen shot*", "cleanshot*"]
Tickets = ["ticket-*", "*-boarding-pass.pdf"]
```

Changes take effect on the next run — no restart required.

## Automation

### systemd user timer

```bash
organize timer --interval 30min --enable
```

This writes `~/.config/systemd/user/file-organizer.{service,timer}` and enables them.
Useful commands:

```bash
systemctl --user list-timers file-organizer.timer
systemctl --user start file-organizer.service       # run now
journalctl --user -u file-organizer.service -n 50  # inspect runs
systemctl --user disable --now file-organizer.timer # pause
systemctl --user enable --now file-organizer.timer  # resume
```

User timers run while your session is active. To keep sorting after logout:

```bash
loginctl enable-linger "$USER"
```

### Watch mode

```bash
organize watch ~/Downloads --interval 60
```

Runs continuously until `Ctrl-C`. The `min_age_minutes` guard still applies.

### Cron

If you prefer cron, add the equivalent of:

```cron
*/30 * * * * /usr/bin/python3 /path/to/organize.py run --apply >/dev/null 2>&1
```

## Command reference

### `organize run [PATH ...]`

| Option | Description |
| --- | --- |
| `--apply` | move files (default is a dry run) |
| `--dest DIR` | collect everything under `DIR` instead of in place |
| `--recursive` / `--no-recursive` | include subfolders |
| `--include-folders` / `--no-include-folders` | move top-level folders |
| `--date-folders` / `--no-date-folders` | `Category/YYYY/MM` |
| `--dedupe` / `--no-dedupe` | move exact duplicates to `Duplicates/` |
| `--sniff` / `--no-sniff` | inspect unknown files with `file(1)` |
| `--min-age MINUTES` | ignore files modified more recently |
| `--exclude GLOB` | extra ignore pattern (repeatable) |
| `-v`, `--verbose` | print each move |
| `--json` | machine-readable plan |
| `--config FILE` | alternate rules file |

### `organize undo [--run ID] [--all] [--apply] [-v]`

Restores a journaled run. Without `--apply` it only previews. `--all` reverses every
recorded run, newest first.

### `organize watch [PATH ...] [--interval SECONDS]`

Same options as `run`, always applied, loops forever.

### `organize config [--init] [--force]`

Shows or creates the rules file.

### `organize timer [--interval 30min] [--enable]`

Installs systemd user units. Without `--enable` it only writes the files.

## Undo and journals

```bash
organize undo                      # preview the latest undo
organize undo --last --apply       # restore the latest run
organize undo --all --apply        # restore every run, newest first
organize undo --run 20261022-2213 --apply
```

Journals live in `~/.local/state/file-organizer/runs/` (set `XDG_STATE_HOME` to move them).
Undo never overwrites: items whose original name is occupied are reported as conflicts and
left in place. Empty category folders created by a run are removed automatically.

## Troubleshooting

| Problem | Solution |
| --- | --- |
| `organize: command not found` | add `~/.local/bin` to `PATH`, or run `python3 /path/to/organize.py` |
| A file was not moved | it is too new, hidden, excluded, a symlink, or an active `.part` download |
| `refusing to organize /home/you` | pass specific folders instead of your home directory |
| `another organize run is in progress` | wait for the current run (usually < 1s) |
| Timer not running | `systemctl --user status file-organizer.timer`; see lingering note above |
| Config error | run `organize config`; the message includes the file path |
| Dry run looks wrong | adjust `[categories]`, `[patterns]`, `targets` or `exclude` |
| Want details of a run | `organize -v` (planned) or `journalctl --user -u file-organizer.service` (timer) |

## Uninstall

```bash
systemctl --user disable --now file-organizer.timer
rm -f ~/.config/systemd/user/file-organizer.{service,timer}
systemctl --user daemon-reload
rm -rf ~/.config/file-organizer ~/.local/state/file-organizer
rm -f ~/.local/bin/organize
```

Remove the cloned directory too. Run `organize undo --all --apply` **before** deleting the
state folder if you want the files moved back.

## Development

```bash
python3 -m unittest -v test_organize.py
```

The suite covers dry runs, classification, name clashes, partial downloads, folder modes,
dedupe, JSON output and undo. CI runs it on Python 3.11–3.13.

Contributions are welcome:

1. Fork and branch.
2. Keep the tool dependency-free and Linux-friendly.
3. Add a test for new behaviour.
4. Open a pull request.

## License

MIT — see [LICENSE](LICENSE).
