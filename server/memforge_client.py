"""Thin subprocess wrapper for the unified memforge entrypoint script.

All memforge interaction goes through a single shell script whose absolute
path is supplied by the caller (typically via ``wiki_search.memforge_script``
or ``wiki_ingest.memforge_reindex_script`` in ``config.json`` — there is no
hardcoded default). The script encapsulates the venv, Python module paths,
and kind registration. Callers only depend on:

  * `memforge.sh search --query - --kind <k> --top-k N --format json`
      stdin = query, stdout = JSON dict from search_docs.
  * `memforge.sh reindex [--kind K] [--rebuild] [--quiet]`
      exit 0 on success, non-zero on failure.

Failures raise `MemforgeError`; callers may fall back to a local backend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class MemforgeError(RuntimeError):
    """Raised when memforge subprocess fails or returns unusable output."""


def _merge_env(extra_targets: dict[str, str] | None) -> dict[str, str] | None:
    """Build a subprocess env with MEMFORGE_EXTRA_TARGETS merged in.

    Returns None when no extras are provided — in that case the child inherits
    the parent env as usual. ``extra_targets`` values are joined into the same
    ``kind:path,kind:path`` format the script already recognises.
    """
    if not extra_targets:
        return None
    pairs = [f"{kind}:{path}" for kind, path in extra_targets.items() if kind and path]
    if not pairs:
        return None
    env = dict(os.environ)
    existing = env.get("MEMFORGE_EXTRA_TARGETS", "").strip()
    merged = ",".join([p for p in (existing,) if p] + pairs)
    env["MEMFORGE_EXTRA_TARGETS"] = merged
    return env


async def memforge_search(
    query: str,
    *,
    kind: str,
    top_k: int,
    timeout: float,
    script_path: str,
    with_keyword: bool = True,
    extra_targets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Invoke `memforge.sh search` and parse the JSON response.

    Raises MemforgeError on any failure (missing script, non-zero exit,
    invalid JSON, timeout). ``extra_targets`` maps kind→absolute-path and is
    forwarded via the ``MEMFORGE_EXTRA_TARGETS`` env var so memforge can see
    roots that are not part of its built-in INDEX_TARGETS (e.g. wiki).
    """
    if not script_path:
        raise MemforgeError("memforge_script path is empty")
    if not os.path.exists(script_path):
        raise MemforgeError(f"memforge script not found: {script_path}")

    args = [
        script_path, "search",
        "--query", "-",
        "--kind", kind,
        "--top-k", str(top_k),
        "--format", "json",
    ]
    if not with_keyword:
        args.append("--no-keyword")

    env = _merge_env(extra_targets)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise MemforgeError(f"failed to launch memforge: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=query.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise MemforgeError(f"memforge search timed out after {timeout}s") from exc

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise MemforgeError(
            f"memforge search exit={proc.returncode}: {err[:400]}"
        )

    out = stdout.decode("utf-8", errors="replace")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise MemforgeError(
            f"memforge search returned invalid JSON: {exc}; head={out[:200]!r}"
        ) from exc


async def memforge_reindex(
    *,
    kind: str | None,
    timeout: float,
    script_path: str,
    quiet: bool = True,
    extra_targets: dict[str, str] | None = None,
) -> int:
    """Invoke `memforge.sh reindex`. Returns the child exit code.

    On process launch failure or timeout raises MemforgeError. A non-zero
    exit code is returned to the caller rather than raised, so ingest hooks
    can log but continue. ``extra_targets`` is forwarded via the
    ``MEMFORGE_EXTRA_TARGETS`` env var, same as ``memforge_search``.
    """
    if not script_path:
        raise MemforgeError("memforge_reindex_script path is empty")
    if not os.path.exists(script_path):
        raise MemforgeError(f"memforge script not found: {script_path}")

    args = [script_path, "reindex"]
    if kind:
        args.extend(["--kind", kind])
    if quiet:
        args.append("--quiet")

    env = _merge_env(extra_targets)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise MemforgeError(f"failed to launch memforge reindex: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise MemforgeError(f"memforge reindex timed out after {timeout}s") from exc

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        logger.warning("[memforge] reindex exit=%s: %s", proc.returncode, err[:400])
    else:
        out = stdout.decode("utf-8", errors="replace").strip()
        if out:
            logger.info("[memforge] reindex: %s", out[:400])

    return proc.returncode
