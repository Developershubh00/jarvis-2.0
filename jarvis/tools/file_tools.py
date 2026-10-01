"""File tools: read, write, edit and list files."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import ToolContext, ToolError, ToolResult, require, short

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", ".next", ".cache", ".gradle", ".idea", "Pods", "DerivedData"}
TEXTUTIL_EXTS = {".docx", ".doc", ".rtf", ".rtfd", ".odt", ".html", ".htm", ".webarchive", ".wordml"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
READ_CHAR_BUDGET = 12_000
MAX_TEXT_FILE = 20 * 1024 * 1024

# Files Jarvis created during this session may be rewritten without asking again.
_created_this_session: set[str] = set()


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def resolve_path(ctx: ToolContext, raw: str | None) -> Path:
    if raw is None or not str(raw).strip():
        raise ToolError("A path is required.")
    p = Path(os.path.expanduser(str(raw).strip()))
    if not p.is_absolute():
        p = ctx.workspace / p
    return Path(os.path.normpath(str(p)))


def is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def is_protected(path: Path) -> bool:
    home = str(Path.home())
    blocked = [home + "/.ssh", home + "/.gnupg", home + "/.aws", home + "/Library/Keychains",
               "/System", "/bin", "/sbin", "/usr", "/etc", "/private/etc", "/var", "/private/var",
               "/Library/Keychains", "/cores", "/dev"]
    allowed = ["/usr/local", "/var/folders", "/private/var/folders", "/tmp", "/private/tmp"]
    candidates = {str(path)}
    try:
        candidates.add(str(Path(path).resolve()))
    except OSError:
        pass
    for cand in candidates:
        if any(_under(cand, a) for a in allowed):
            continue
        if any(_under(cand, b) for b in blocked):
            return True
    return False


def _backup(ctx: ToolContext, path: Path) -> Path | None:
    """Keep a copy of a file outside the workspace before changing it."""
    try:
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            return None
        folder = Path(ctx.cfg.paths.backups) / time.strftime("%Y%m%d-%H%M%S")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / path.name
        shutil.copy2(path, target)
        return target
    except OSError:
        return None


def check_write_allowed(ctx: ToolContext, path: Path, action: str) -> Path | None:
    """Raise ToolError if a write isn't allowed. Returns a backup path when one was made."""
    if is_protected(path):
        raise ToolError(f"Writing to {path} is blocked for safety (system or credentials location).")
    if not path.exists() or is_within(path, ctx.workspace) or str(path) in _created_this_session:
        return None
    if getattr(ctx.cfg.safety, "confirm_writes_outside_workspace", True):
        ok = ctx.confirm("Jarvis wants to change a file", f"{action} this file?\n\n{path}")
        if not ok:
            raise ToolError("The user declined this change. Don't retry it; ask what they'd prefer.")
    return _backup(ctx, path)


# ------------------------------------------------------------------ read

def _number_lines(text: str, path: Path, start_line: int) -> str:
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return f"{path} is empty."
    start = min(max(1, start_line), total)
    out: list[str] = []
    used = 0
    end = start - 1
    for i in range(start, total + 1):
        line = lines[i - 1]
        if len(line) > 2000:
            line = line[:2000] + " [line truncated]"
        entry = f"{i:>5}\t{line}"
        if out and used + len(entry) > READ_CHAR_BUDGET:
            break
        out.append(entry)
        used += len(entry) + 1
        end = i
    body = "\n".join(out)
    if start > 1 or end < total:
        body += f"\n\n(lines {start}-{end} of {total}"
        body += f"; call read_file again with start_line={end + 1} to continue)" if end < total else ")"
    return body


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ToolError("PDF support needs the pypdf package (pip install pypdf).") from e
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:
                raise ToolError("This PDF is password-protected.") from e
        parts = []
        for i, page in enumerate(reader.pages[:80], start=1):
            parts.append(f"--- page {i} ---\n{(page.extract_text() or '').strip()}")
        if len(reader.pages) > 80:
            parts.append(f"(stopped after 80 of {len(reader.pages)} pages)")
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Couldn't read the PDF: {e}") from e
    text = "\n\n".join(parts)
    if not text.replace("--- page", "").strip(" -0123456789\n"):
        return text + "\n\n(No selectable text found: it may be a scanned PDF. Open it and use take_screenshot to read it.)"
    return text


def _textutil(path: Path) -> str:
    if sys.platform != "darwin":
        raise ToolError(f"Reading {path.suffix} files needs macOS (textutil).")
    proc = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                          capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise ToolError("textutil couldn't convert the file: " + proc.stderr.decode(errors="replace").strip())
    return proc.stdout.decode("utf-8", errors="replace")


def _read_image(path: Path) -> ToolResult:
    from PIL import Image

    try:
        img = Image.open(path)
        img.load()
    except Exception:
        if sys.platform != "darwin":
            raise ToolError(f"Couldn't open the image {path.name}.")
        tmp = Path("/tmp") / f"jarvis-img-{os.getpid()}.png"
        proc = subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(tmp)],
                              capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise ToolError(f"Couldn't open the image {path.name}.")
        img = Image.open(tmp)
        img.load()
    original = img.size
    img = img.convert("RGB")
    img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return ToolResult(text=f"Image {path.name} ({original[0]}x{original[1]} px).", image_jpeg=buf.getvalue())


def read_file(ctx: ToolContext, a: dict) -> ToolResult | str:
    path = resolve_path(ctx, require(a, "path"))
    if not path.exists():
        raise ToolError(f"No such file: {path}")
    if path.is_dir():
        raise ToolError(f"{path} is a folder. Use list_dir to see what's inside.")
    try:
        start_line = int(a.get("start_line") or 1)
    except (TypeError, ValueError):
        start_line = 1
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
        if ext == ".pdf":
            return _number_lines(_read_pdf(path), path, start_line)
        if ext in TEXTUTIL_EXTS:
            return _number_lines(_textutil(path), path, start_line)
        if ext in IMAGE_EXTS:
            return _read_image(path)
        if size > MAX_TEXT_FILE:
            raise ToolError(f"{path.name} is {human_size(size)}; too big to read whole. Use run_shell with head, tail or grep.")
        with open(path, "rb") as f:
            raw = f.read()
    except PermissionError as e:
        raise ToolError(f"macOS blocked access to {path}. Allow your terminal app under System Settings > "
                        "Privacy & Security > Files and Folders (or Full Disk Access).") from e
    if b"\x00" in raw[:8192]:
        raise ToolError(f"{path.name} looks like a binary file ({human_size(size)}); I can't read it as text.")
    return _number_lines(raw.decode("utf-8", errors="replace"), path, start_line)


# ------------------------------------------------------------------ write / edit

def write_file(ctx: ToolContext, a: dict) -> str:
    path = resolve_path(ctx, require(a, "path"))
    content = a.get("content")
    if content is None:
        raise ToolError("'content' is required (use an empty string for an empty file).")
    content = str(content)
    append = bool(a.get("append"))
    if path.is_dir():
        raise ToolError(f"{path} is a folder.")
    backup = check_write_allowed(ctx, path, "Append to" if append else "Overwrite")
    existed = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a" if append else "w", encoding="utf-8") as f:
            f.write(content)
    except PermissionError as e:
        raise ToolError(f"Permission denied writing {path}.") from e
    if not existed:
        _created_this_session.add(str(path))
    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    verb = "Appended to" if append else ("Overwrote" if existed else "Created")
    msg = f"{verb} {path} ({lines} lines, {human_size(len(content.encode('utf-8')))})."
    if backup:
        msg += f" Backup of the old version: {backup}"
    return msg


def edit_file(ctx: ToolContext, a: dict) -> str:
    path = resolve_path(ctx, require(a, "path"))
    old = a.get("old_text")
    new = a.get("new_text")
    if not old:
        raise ToolError("'old_text' is required: the exact text to replace.")
    if new is None:
        raise ToolError("'new_text' is required (an empty string deletes old_text).")
    if not path.is_file():
        raise ToolError(f"No such file: {path}. Use write_file to create it.")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ToolError(f"{path.name} isn't UTF-8 text.") from e
    count = text.count(old)
    replace_all = bool(a.get("replace_all"))
    if count == 0:
        hint = ""
        stripped = old.strip()
        if stripped and stripped in text:
            hint = " The text exists with different surrounding whitespace; copy it exactly from read_file."
        raise ToolError("old_text was not found in the file." + (hint or " Read the file again and copy the text exactly, including indentation."))
    if count > 1 and not replace_all:
        raise ToolError(f"old_text matches {count} places. Include more surrounding lines so it's unique, or set replace_all to true.")
    backup = check_write_allowed(ctx, path, "Edit")
    line = text[: text.index(old)].count("\n") + 1
    updated = text.replace(old, str(new)) if replace_all else text.replace(old, str(new), 1)
    path.write_text(updated, encoding="utf-8")
    msg = f"Edited {path}: replaced {count if replace_all else 1} occurrence(s) starting at line {line}."
    if backup:
        msg += f" Backup: {backup}"
    return msg


# ------------------------------------------------------------------ list

def list_dir(ctx: ToolContext, a: dict) -> str:
    path = resolve_path(ctx, a.get("path") or ".")
    if not path.exists():
        raise ToolError(f"No such folder: {path}")
    if not path.is_dir():
        raise ToolError(f"{path} is a file. Use read_file to read it.")
    try:
        depth = max(1, min(4, int(a.get("depth") or 1)))
    except (TypeError, ValueError):
        depth = 1
    show_hidden = bool(a.get("show_hidden"))
    limit = 400
    lines = [f"{path}/"]
    count = 0

    def walk(folder: str, level: int, indent: str) -> None:
        nonlocal count
        try:
            entries = sorted(os.scandir(folder), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except PermissionError:
            lines.append(indent + "(macOS blocked access: allow your terminal app under Privacy & Security > Files and Folders)")
            return
        except OSError as e:
            lines.append(indent + f"({e.strerror})")
            return
        for entry in entries:
            if count >= limit:
                return
            if not show_hidden and entry.name.startswith("."):
                continue
            count += 1
            if entry.is_dir(follow_symlinks=False):
                if entry.name in SKIP_DIRS:
                    lines.append(f"{indent}{entry.name}/ (not expanded)")
                    continue
                lines.append(f"{indent}{entry.name}/")
                if level < depth:
                    walk(entry.path, level + 1, indent + "  ")
            else:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                lines.append(f"{indent}{entry.name}  ({human_size(size)})")

    walk(str(path), 1, "  ")
    if count >= limit:
        lines.append(f"  ... stopped after {limit} entries")
    elif count == 0:
        lines.append("  (empty)")
    return "\n".join(lines)


def register(reg) -> None:
    reg.add(
        "read_file",
        """Read a file. Text and code come back with line numbers. Also reads PDF, Word (.docx/.doc),
        RTF, ODT and HTML as text, and images (png, jpg, heic...) so you can see them.
        Relative paths are inside the workspace. Long files are returned in chunks: use start_line to continue.""",
        {
            "path": {"type": "string", "description": "File path (absolute, ~/..., or relative to the workspace)."},
            "start_line": {"type": "integer", "description": "First line to show (1-based). Default 1."},
        },
        required=("path",), func=read_file,
        status=lambda a: f"Reading {short(Path(str(a.get('path', ''))).name or a.get('path', ''), 60)}",
        activity="Reading a file…",
    )
    reg.add(
        "write_file",
        """Create or overwrite a text file (parent folders are created). Use append=true to add to the end,
        which is how you write very large files in several parts. Relative paths go in the workspace.
        Changing existing files outside the workspace asks the user first and keeps a backup.""",
        {
            "path": {"type": "string", "description": "File path."},
            "content": {"type": "string", "description": "Full text to write."},
            "append": {"type": "boolean", "description": "Append instead of overwrite. Default false."},
        },
        required=("path", "content"), func=write_file,
        status=lambda a: ("Adding to " if a.get("append") else "Writing ") + short(Path(str(a.get("path", ""))).name, 60),
        activity="Writing a file…",
    )
    reg.add(
        "edit_file",
        """Replace exact text in a file. old_text must match the file exactly (including indentation) and be
        unique unless replace_all is true. Read the file first. Best for small, targeted changes.""",
        {
            "path": {"type": "string", "description": "File path."},
            "old_text": {"type": "string", "description": "Exact existing text to replace."},
            "new_text": {"type": "string", "description": "Replacement text (empty string deletes)."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."},
        },
        required=("path", "old_text", "new_text"), func=edit_file,
        status=lambda a: "Editing " + short(Path(str(a.get("path", ""))).name, 60),
        activity="Editing a file…",
    )
    reg.add(
        "list_dir",
        """List a folder's contents with file sizes, optionally recursing (depth up to 4). Skips .git,
        node_modules and virtualenvs. Hidden files only with show_hidden.""",
        {
            "path": {"type": "string", "description": "Folder path. Default: the workspace."},
            "depth": {"type": "integer", "description": "How many levels deep (1-4). Default 1."},
            "show_hidden": {"type": "boolean", "description": "Include dotfiles. Default false."},
        },
        func=list_dir,
        status=lambda a: "Looking in " + short(a.get("path") or "the workspace", 60),
        activity="Looking at files…",
    )
