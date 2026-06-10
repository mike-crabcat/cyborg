"""Workspace file tools for LLM function calling.

Usage:
    tools = make_workspace_tools(ctx)
    result = await dispatch.chat_with_tools(messages, tools, ...)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
from pathlib import Path

from cyborg_server.context import AppContext
from cyborg_server.services.skill_env import build_skill_env
from cyborg_server.services.tools import ImageInjection, tool

logger = logging.getLogger(__name__)

_MAX_LIST_ENTRIES = 100
_MAX_READ_BYTES = 50 * 1024
_MAX_WRITE_BYTES = 100 * 1024
_SCRIPT_TIMEOUT_SECONDS = 900


def _resolve_path(ctx: AppContext, path: str) -> Path:
    """Resolve a path against the workspace dir.

    Accepts both absolute and workspace-relative paths:
    - Absolute path: must be within the workspace, returned as-is after validation.
    - Relative path: resolved against workspace root (backward compatible).
    """
    workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
    parsed = Path(path)
    if ".." in parsed.parts:
        raise ValueError(f"Path '{path}' escapes workspace directory")
    if parsed.is_absolute():
        resolved = parsed.resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError:
            raise ValueError(f"Path '{path}' is outside the workspace directory")
        return resolved
    return workspace / path


def make_workspace_tools(ctx: AppContext, *, session_key: str | None = None):
    """Create workspace file tools bound to the given context.

    If session_key is provided, also includes an update_agenda tool.
    """

    @tool
    async def ls(
        path: str = "",
    ) -> str:
        """List files and directories in a single workspace directory (non-recursive). Path can be absolute (within workspace) or relative to workspace root."""
        workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
        target = _resolve_path(ctx, path) if path else workspace
        if not target.is_dir():
            return json.dumps({"error": f"'{path}' is not a directory"})

        entries: list[dict] = []
        try:
            children = sorted(target.iterdir())
        except PermissionError:
            return json.dumps({"error": f"permission denied: '{path}'"})

        for child in children:
            if len(entries) >= _MAX_LIST_ENTRIES:
                entries.append({"name": "...", "type": "truncated"})
                break
            entry: dict = {
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
            }
            if child.is_file():
                try:
                    entry["size_bytes"] = child.stat().st_size
                except OSError:
                    pass
            entries.append(entry)

        return json.dumps(entries)

    @tool
    async def read_file(
        path: str,
    ) -> str:
        """Read the contents of a file in the workspace. Path can be absolute (within workspace) or relative to workspace root."""
        resolved = _resolve_path(ctx, path)
        if not resolved.is_file():
            return f"Error: '{path}' is not a file"

        # Detect image files and provide a helpful message
        suffix = resolved.suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
            size = resolved.stat().st_size
            return f"Image file ({suffix}, {size} bytes). Images cannot be read as text."

        size = resolved.stat().st_size
        if size > _MAX_READ_BYTES:
            return f"Error: file is {size} bytes (max {_MAX_READ_BYTES})"

        content = resolved.read_bytes()
        if b"\x00" in content[:8192]:
            return "Error: binary file"

        return content.decode("utf-8")

    @tool
    async def read_image(
        path: str,
    ) -> ImageInjection:
        """Load an image from the workspace so you can see and analyze it. Supports PNG, JPG, GIF, WebP, and BMP. Path can be absolute (within workspace) or relative to workspace root."""
        resolved = _resolve_path(ctx, path)
        if not resolved.is_file():
            return ImageInjection(text=f"Error: '{path}' is not a file", data_url="")

        suffix = resolved.suffix.lower()
        _MIME_MAP = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        }
        mime = _MIME_MAP.get(suffix)
        if not mime:
            return ImageInjection(text=f"Error: '{path}' is not a supported image format (use png, jpg, gif, webp, or bmp)", data_url="")

        data = resolved.read_bytes()
        b64 = base64.b64encode(data).decode()
        return ImageInjection(
            text=f"Image loaded from {path} ({len(data)} bytes)",
            data_url=f"data:{mime};base64,{b64}",
        )

    @tool
    async def write_file(
        path: str,
        content: str,
    ) -> str:
        """Write content to a file in the workspace. Path can be absolute (within workspace) or relative to workspace root. Creates parent directories if needed."""
        if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
            return f"Error: content exceeds {_MAX_WRITE_BYTES} bytes"

        workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
        resolved = _resolve_path(ctx, path)

        # Guard memory directory — use memory_write tool instead to keep indexes in sync
        if str(resolved).startswith(str(workspace / "memory")):
            return "Error: Use the memory_write tool to modify memory entries (ensures the index stays in sync)"
        if resolved.exists() and resolved.is_dir():
            return f"Error: '{path}' is a directory"

        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        logger.info("Workspace write: %s (%d bytes)", path, len(content))
        return json.dumps({"ok": True, "path": path, "bytes": len(content.encode("utf-8"))})

    @tool
    async def rm(
        path: str,
        recursive: bool = False,
    ) -> str:
        """Delete a file or directory in the workspace. Path can be absolute (within workspace) or relative to workspace root. Set recursive=true to delete non-empty directories."""
        workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
        resolved = _resolve_path(ctx, path)
        if resolved == workspace:
            return "Error: cannot delete workspace root"
        if not resolved.exists():
            return f"Error: '{path}' does not exist"
        if resolved.is_file():
            resolved.unlink()
            logger.info("Workspace delete: %s", path)
            return json.dumps({"ok": True, "path": path})
        if resolved.is_dir():
            if not recursive and any(resolved.iterdir()):
                return f"Error: '{path}' is a non-empty directory (set recursive=true to delete)"
            shutil.rmtree(resolved)
            logger.info("Workspace delete (recursive): %s", path)
            return json.dumps({"ok": True, "path": path})
        return f"Error: '{path}' is not a file or directory"

    @tool
    async def mv(
        source: str,
        destination: str,
    ) -> str:
        """Move or rename a file or directory into the workspace. Source can be any accessible path (e.g. incoming attachments). Destination must be within the workspace. Creates destination parent directories if needed."""
        src = Path(source).resolve()
        dst = _resolve_path(ctx, destination)
        if not src.exists():
            return f"Error: '{source}' does not exist"
        if dst.exists():
            return f"Error: '{destination}' already exists"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        logger.info("Workspace move: %s -> %s", source, destination)
        return json.dumps({"ok": True, "from": source, "to": destination})

    @tool
    async def cp(
        source: str,
        destination: str,
    ) -> str:
        """Copy a file or directory into the workspace. Source can be any accessible path (e.g. incoming attachments). Destination must be within the workspace. Creates destination parent directories if needed."""
        src = Path(source).resolve()
        dst = _resolve_path(ctx, destination)
        if not src.exists():
            return f"Error: '{source}' does not exist"
        if dst.exists():
            return f"Error: '{destination}' already exists"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
        logger.info("Workspace copy: %s -> %s", source, destination)
        return json.dumps({"ok": True, "from": source, "to": destination})

    @tool
    async def find(
        query: str = "",
        pattern: str = "",
        path: str = "",
    ) -> str:
        """Search workspace files. Use 'pattern' to find files by name (e.g. '*.py', 'test_*'), 'query' to search file contents, or both. At least one is required. Optional 'path' scopes the search to a subdirectory."""
        if not query and not pattern:
            return "Error: provide at least one of 'query' or 'pattern'"

        workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
        search_root = _resolve_path(ctx, path) if path else workspace
        if not search_root.is_dir():
            return json.dumps({"error": f"'{path}' is not a directory"})

        _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache"}
        _MAX_RESULTS = 20

        results: list[dict] = []

        def _matches_name(file_path: Path) -> bool:
            if not pattern:
                return True
            from fnmatch import fnmatch
            return fnmatch(file_path.name, pattern)

        def _walk(dir_path: Path) -> None:
            if len(results) >= _MAX_RESULTS:
                return
            try:
                children = sorted(dir_path.iterdir())
            except PermissionError:
                return
            for child in children:
                if len(results) >= _MAX_RESULTS:
                    return
                if child.is_dir():
                    if child.name not in _SKIP_DIRS:
                        _walk(child)
                elif child.is_file() and _matches_name(child):
                    if not query:
                        results.append({"path": str(child.relative_to(workspace))})
                    else:
                        try:
                            raw = child.read_bytes()
                            if b"\x00" in raw[:8192]:
                                continue
                            text = raw.decode("utf-8", errors="replace")
                        except OSError:
                            continue
                        for i, line in enumerate(text.splitlines(), 1):
                            if query.lower() in line.lower():
                                results.append({
                                    "path": str(child.relative_to(workspace)),
                                    "line": i,
                                    "text": line.strip()[:200],
                                })
                                if len(results) >= _MAX_RESULTS:
                                    return

        _walk(search_root)
        if not results:
            return json.dumps({"results": [], "message": "no matches found"})
        return json.dumps({"results": results})

    @tool
    async def run_script(
        path: str,
        args: list[str] = [],
    ) -> str:
        """Run a Python script in the workspace. Path can be absolute (within workspace) or relative to workspace root.
        The script runs via `uv run` from its parent directory (so per-script pyproject.toml works).
        Returns the script's stdout. Args are passed as command-line arguments."""
        resolved = _resolve_path(ctx, path)
        if not resolved.is_file():
            return f"Error: '{path}' is not a file"
        if not resolved.suffix == ".py":
            return f"Error: '{path}' is not a Python file"

        uv_bin = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
        cmd = [uv_bin, "run", str(resolved.name)]
        if args:
            cmd.extend(args)

        logger.info("run_script: %s %s", path, args)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(resolved.parent),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=build_skill_env(
                    workspace_dir=str(ctx.settings.harness.workspace_dir.expanduser().resolve()),
                ),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=_SCRIPT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: script timed out after {_SCRIPT_TIMEOUT_SECONDS}s"

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return f"Error (exit code {proc.returncode}):\n{err or out}"

        return out

    @tool
    async def use_skill(
        skill_name: str,
    ) -> str:
        """Load the full instructions for a skill by name. Returns the skill's instructions
        and the path to its directory so you can call run_script with correct paths."""
        from cyborg_server.services.skill_loader import load_skill
        return load_skill(ctx.settings.harness.workspace_dir, skill_name)

    tools = [ls, read_file, read_image, write_file, rm, mv, cp, find, run_script, use_skill]

    if session_key:
        @tool
        async def update_agenda(agenda: str) -> str:
            """Update the agenda for this session. The agenda extends the system prompt,
            guiding your behavior for all subsequent turns. Replace the full agenda text —
            use this to mark tasks complete, add new goals, or change your instructions."""
            from cyborg_server.services.session_agenda_service import SessionAgendaService
            await SessionAgendaService(ctx).set_agenda(session_key, agenda)
            return json.dumps({"ok": True})

        tools.append(update_agenda)

    return tools
