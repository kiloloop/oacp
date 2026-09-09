#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Unified preflight checks for OACP.

Fast mode (default):
- Merge conflict marker scan
- Makefile `.PHONY`/target consistency checks
- Packaging boundary: `scripts/` contents == wheel force-include entries
- YAML syntax validation for `templates/` and `docs/protocol/`
- `ruff` on all tracked Python files
- `shellcheck` on all tracked `scripts/**/*.sh`

Extended mode (`--full`): fast mode + `make test`.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

Runner = Callable[[Sequence[str], Path], Tuple[int, str]]
YamlLoader = Callable[[str], object]

MARKER_PREFIXES = ("<<<<<<<", "=======", ">>>>>>>")
YAML_EXTENSIONS = {".yaml", ".yml"}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: str
    duration_s: float

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


def run_command(command: Sequence[str], cwd: Path) -> Tuple[int, str]:
    """Run a command and return (exit_code, combined_output)."""
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"Command not found: {command[0]}"

    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return completed.returncode, combined.strip()


def _iter_logical_makefile_lines(raw: str) -> Iterable[str]:
    """Yield Makefile lines with trailing backslash continuations collapsed."""
    buffer = ""
    for physical in raw.splitlines():
        line = physical.rstrip()
        current = f"{buffer}{line.lstrip()}" if buffer else line
        if current.endswith("\\"):
            buffer = current[:-1] + " "
            continue
        yield current
        buffer = ""
    if buffer:
        yield buffer


def validate_makefile_phony(makefile_path: Path) -> Tuple[List[str], List[str], List[str], List[str]]:
    """Return (defined, phony, missing_phony, orphan_phony)."""
    raw = makefile_path.read_text(encoding="utf-8")
    target_pattern = re.compile(r"^([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*)\s*:(?![=])")
    phony_pattern = re.compile(r"^\.PHONY\s*:\s*(.+)$")

    defined = set()
    phony = set()

    for line in _iter_logical_makefile_lines(raw):
        if not line or line.startswith("\t"):
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        phony_match = phony_pattern.match(stripped)
        if phony_match:
            phony.update(part for part in phony_match.group(1).split() if part)
            continue

        target_match = target_pattern.match(line)
        if target_match:
            for target in target_match.group(1).split():
                if target != ".PHONY":
                    defined.add(target)

    public_defined = {
        target for target in defined if not target.startswith("_") and not target.startswith(".")
    }
    missing_phony = sorted(public_defined - phony)
    orphan_phony = sorted(phony - defined)
    return sorted(defined), sorted(phony), missing_phony, orphan_phony


def check_makefile(repo_root: Path) -> CheckResult:
    start = time.monotonic()
    makefile_path = repo_root / "Makefile"
    if not makefile_path.is_file():
        return CheckResult(
            name="makefile-parse",
            passed=False,
            details="Makefile not found",
            duration_s=time.monotonic() - start,
        )

    defined, phony, missing_phony, orphan_phony = validate_makefile_phony(makefile_path)
    if not phony:
        return CheckResult(
            name="makefile-parse",
            passed=False,
            details="Makefile has no .PHONY targets",
            duration_s=time.monotonic() - start,
        )

    if missing_phony or orphan_phony:
        detail_lines: List[str] = []
        if missing_phony:
            detail_lines.append(
                "Targets defined but missing from .PHONY: " + ", ".join(missing_phony)
            )
        if orphan_phony:
            detail_lines.append(
                "Targets listed in .PHONY but not defined: " + ", ".join(orphan_phony)
            )
        return CheckResult(
            name="makefile-parse",
            passed=False,
            details="\n".join(detail_lines),
            duration_s=time.monotonic() - start,
        )

    return CheckResult(
        name="makefile-parse",
        passed=True,
        details=f"validated {len(defined)} targets and {len(phony)} .PHONY entries",
        duration_s=time.monotonic() - start,
    )


FORCE_INCLUDE_HEADER = "[tool.hatch.build.targets.wheel.force-include]"
_FORCE_INCLUDE_ENTRY = re.compile(r'^"([^"]+)"\s*=\s*"([^"]+)"$')


def parse_force_include(pyproject_path: Path) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Parse the wheel force-include table line-wise.

    Returns (entries, errors) with entries in file order. Line-wise parsing
    keeps the check dependency-free on Python < 3.11 (no tomllib); the table
    format is enforced as one `"source" = "destination"` pair per line.
    """
    raw = pyproject_path.read_text(encoding="utf-8")
    entries: List[Tuple[str, str]] = []
    errors: List[str] = []
    seen_sources = set()
    in_table = False
    table_found = False

    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if stripped == FORCE_INCLUDE_HEADER:
            in_table = True
            table_found = True
            continue
        if not in_table:
            continue
        if stripped.startswith("["):
            in_table = False
            continue
        if not stripped or stripped.startswith("#"):
            continue
        match = _FORCE_INCLUDE_ENTRY.match(stripped)
        if not match:
            errors.append(
                f"pyproject.toml:{lineno}: unparseable force-include line: {stripped[:60]}"
            )
            continue
        source, destination = match.group(1), match.group(2)
        if source in seen_sources:
            errors.append(
                f"pyproject.toml:{lineno}: duplicate force-include source: {source}"
            )
            continue
        seen_sources.add(source)
        entries.append((source, destination))

    if not table_found:
        errors.append(f"pyproject.toml: missing {FORCE_INCLUDE_HEADER} table")
    return entries, errors


def _iter_script_files(repo_root: Path) -> List[str]:
    """Repo-relative POSIX paths of all regular files under scripts/."""
    scripts_root = repo_root / "scripts"
    if not scripts_root.is_dir():
        return []
    files: List[str] = []
    for path in sorted(scripts_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        files.append(rel.as_posix())
    return files


def check_packaging_boundary(repo_root: Path) -> CheckResult:
    """Enforce `scripts/` == force-include: every script is packaged and every
    force-include source exists on disk. Fails on drift in either direction."""
    start = time.monotonic()
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return CheckResult(
            name="packaging-boundary",
            passed=False,
            details="pyproject.toml not found",
            duration_s=time.monotonic() - start,
        )

    entries, problems = parse_force_include(pyproject_path)
    sources = [source for source, _ in entries]

    scripts_on_disk = set(_iter_script_files(repo_root))
    script_sources = {source for source in sources if source.startswith("scripts/")}

    unpackaged = sorted(scripts_on_disk - script_sources)
    if unpackaged:
        problems.append(
            "scripts/ files missing from force-include: " + ", ".join(unpackaged)
        )

    missing_files = sorted(
        source for source in sources if not (repo_root / source).is_file()
    )
    if missing_files:
        problems.append(
            "force-include sources with no file on disk: " + ", ".join(missing_files)
        )

    if problems:
        return CheckResult(
            name="packaging-boundary",
            passed=False,
            details="\n".join(problems),
            duration_s=time.monotonic() - start,
        )

    return CheckResult(
        name="packaging-boundary",
        passed=True,
        details=(
            f"scripts/ ({len(scripts_on_disk)} files) matches force-include; "
            f"all {len(sources)} sources exist"
        ),
        duration_s=time.monotonic() - start,
    )


# The memory engine lives outside the kernel (`agent-memory-cli`). No kernel
# module may import it, in either of its spellings: the retired in-tree
# `memory_*` modules or the tool's `agent_memory` package. The check walks the
# parsed module rather than matching lines, so every alias of a comma-list
# import, semicolon-joined statements, relative (`from . import memory_sync`)
# and qualified (`oacp._scripts.memory_sync`) forms, function-local imports,
# and a dynamic import by string literal, whether the literal is the module
# or the package it resolves against, are all caught, while a string or
# comment that merely names a module is not.
MEMORY_MODULE_RE = re.compile(r"^(?:memory_\w*|agent_memory)$")
# Parameters of the dynamic importers that name a module, as (keyword,
# position): `import_module(name, package=None)` and `__import__(name,
# globals, locals, fromlist, level)`. Only literal strings are checkable.
DYNAMIC_IMPORT_MODULE_PARAMS = {
    "import_module": (("name", 0), ("package", 1)),
    "__import__": (("name", 0), ("fromlist", 3)),
}
KERNEL_MODULE_DIRS = ("oacp", "scripts")


def _iter_kernel_modules(repo_root: Path) -> List[Path]:
    modules: List[Path] = []
    for dirname in KERNEL_MODULE_DIRS:
        root = repo_root / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(repo_root)
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
                continue
            modules.append(path)
    return modules


def _names_memory_engine(dotted: str) -> bool:
    return any(MEMORY_MODULE_RE.match(part) for part in dotted.split("."))


def _call_argument(node: ast.Call, keyword: str, position: int) -> Optional[ast.AST]:
    """The argument passed for one parameter, positionally or by keyword."""
    if position < len(node.args):
        return node.args[position]
    return next((kw.value for kw in node.keywords if kw.arg == keyword), None)


def _literal_strings(node: Optional[ast.AST]) -> List[str]:
    """String constants in a literal, or in the elements of a literal list/tuple."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [text for elt in node.elts for text in _literal_strings(elt)]
    return []


def _imported_names(node: ast.AST) -> List[str]:
    """Dotted names an import-shaped node resolves; [] for any other node."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        # `from pkg.memory_sync import x` names the module on the left;
        # `from pkg import memory_sync` and `from . import memory_sync` name
        # it as the imported item, so both sides are checked.
        return [node.module or "", *(alias.name for alias in node.names)]
    if isinstance(node, ast.Call):
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return [
            name
            for keyword, position in DYNAMIC_IMPORT_MODULE_PARAMS.get(callee, ())
            for name in _literal_strings(_call_argument(node, keyword, position))
        ]
    return []


def memory_engine_imports(source: str, rel: str) -> List[str]:
    """`<rel>:<line>: <statement>` for each import in `source` naming the engine.

    A module that does not parse is reported as a hit: an import the guard
    cannot see is not one it can vouch for.
    """
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return [f"{rel}:{exc.lineno or 0}: unparseable ({exc.msg})"]
    lines = source.splitlines()
    hits: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if any(_names_memory_engine(name) for name in _imported_names(node)):
            hits.append((node.lineno, lines[node.lineno - 1].strip()[:80]))
    return [f"{rel}:{lineno}: {text}" for lineno, text in sorted(set(hits))]


def check_memory_boundary(repo_root: Path) -> CheckResult:
    """No kernel module imports the memory engine (`memory_*` or `agent_memory`)."""
    start = time.monotonic()
    hits: List[str] = []
    modules = _iter_kernel_modules(repo_root)
    for path in modules:
        rel = path.relative_to(repo_root).as_posix()
        source = path.read_text(encoding="utf-8", errors="replace")
        hits.extend(memory_engine_imports(source, rel))
    if hits:
        return CheckResult(
            name="memory-boundary",
            passed=False,
            details="kernel modules importing the memory engine:\n" + "\n".join(hits),
            duration_s=time.monotonic() - start,
        )
    return CheckResult(
        name="memory-boundary",
        passed=True,
        details=f"{len(modules)} kernel modules import no memory engine",
        duration_s=time.monotonic() - start,
    )


def _discover_repo_files(repo_root: Path, runner: Runner) -> List[Path]:
    rc, output = runner(["git", "ls-files"], repo_root)
    if rc == 0:
        files: List[Path] = []
        for rel in output.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            path = repo_root / rel
            if path.is_file():
                files.append(path)
        return files

    # Fallback for non-git environments.
    files = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(repo_root).parts):
            continue
        files.append(path)
    return files


def check_conflict_markers(repo_root: Path, runner: Runner = run_command) -> CheckResult:
    start = time.monotonic()
    hits: List[str] = []

    for file_path in _discover_repo_files(repo_root, runner):
        rel = file_path.relative_to(repo_root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue

        try:
            raw = file_path.read_bytes()
        except OSError:
            continue

        if b"\x00" in raw:
            continue

        for lineno, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
            stripped = line.strip()
            if (
                stripped.startswith(MARKER_PREFIXES[0])
                or stripped == MARKER_PREFIXES[1]
                or stripped.startswith(MARKER_PREFIXES[2])
            ):
                preview = stripped[:60]
                hits.append(f"{rel}:{lineno}: {preview}")
                if len(hits) >= 20:
                    break
        if len(hits) >= 20:
            break

    if hits:
        shown = "\n".join(hits[:10])
        extra = "" if len(hits) <= 10 else f"\n... and {len(hits) - 10} more"
        return CheckResult(
            name="conflict-markers",
            passed=False,
            details=f"merge conflict markers found:\n{shown}{extra}",
            duration_s=time.monotonic() - start,
        )

    return CheckResult(
        name="conflict-markers",
        passed=True,
        details="no merge conflict markers detected",
        duration_s=time.monotonic() - start,
    )


def discover_yaml_files(repo_root: Path) -> List[Path]:
    yaml_files: List[Path] = []
    for rel_root in ("templates", "docs/protocol"):
        root = repo_root / rel_root
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in YAML_EXTENSIONS:
                yaml_files.append(path)
    return yaml_files


def default_yaml_loader() -> Optional[YamlLoader]:
    try:
        import yaml  # type: ignore
    except Exception:
        return None
    return yaml.safe_load


def check_yaml_syntax(repo_root: Path, loader: Optional[YamlLoader] = None) -> CheckResult:
    start = time.monotonic()
    yaml_files = discover_yaml_files(repo_root)
    if not yaml_files:
        return CheckResult(
            name="yaml-validate",
            passed=True,
            details="no YAML files found under templates/ or docs/protocol/",
            duration_s=time.monotonic() - start,
        )

    yaml_loader = loader or default_yaml_loader()
    if yaml_loader is None:
        return CheckResult(
            name="yaml-validate",
            passed=False,
            details="PyYAML is required for YAML validation (install with `pip install pyyaml`)",
            duration_s=time.monotonic() - start,
        )

    errors: List[str] = []
    for path in yaml_files:
        rel = path.relative_to(repo_root)
        try:
            text = path.read_text(encoding="utf-8")
            yaml_loader(text)
        except Exception as exc:  # pragma: no cover - parser-specific errors
            errors.append(f"{rel}: {exc}")

    if errors:
        shown = "\n".join(errors[:10])
        extra = "" if len(errors) <= 10 else f"\n... and {len(errors) - 10} more"
        return CheckResult(
            name="yaml-validate",
            passed=False,
            details=f"invalid YAML detected:\n{shown}{extra}",
            duration_s=time.monotonic() - start,
        )

    return CheckResult(
        name="yaml-validate",
        passed=True,
        details=f"validated {len(yaml_files)} YAML files",
        duration_s=time.monotonic() - start,
    )


def _run_external_check(
    *,
    name: str,
    command: Sequence[str],
    repo_root: Path,
    runner: Runner,
) -> CheckResult:
    start = time.monotonic()
    tool = command[0]
    if shutil.which(tool) is None:
        return CheckResult(
            name=name,
            passed=False,
            details=f"{tool} is not on PATH",
            duration_s=time.monotonic() - start,
        )

    rc, output = runner(command, repo_root)
    if rc == 0:
        return CheckResult(
            name=name,
            passed=True,
            details="ok",
            duration_s=time.monotonic() - start,
        )

    details = output or f"{tool} exited with code {rc}"
    return CheckResult(
        name=name,
        passed=False,
        details=details,
        duration_s=time.monotonic() - start,
    )


def check_ruff(repo_root: Path, runner: Runner = run_command) -> CheckResult:
    py_files = sorted(
        path
        for path in _discover_repo_files(repo_root, runner)
        if path.suffix == ".py"
        and not any(part in SKIP_DIRS for part in path.relative_to(repo_root).parts)
    )
    if not py_files:
        return CheckResult(
            name="ruff",
            passed=True,
            details="no tracked Python files",
            duration_s=0.0,
        )

    rel_files = [str(path.relative_to(repo_root)) for path in py_files]
    return _run_external_check(
        name="ruff",
        command=["ruff", "check", *rel_files],
        repo_root=repo_root,
        runner=runner,
    )


def check_shellcheck(repo_root: Path, runner: Runner = run_command) -> CheckResult:
    shell_files = sorted((repo_root / "scripts").rglob("*.sh"))
    if not shell_files:
        return CheckResult(
            name="shellcheck",
            passed=True,
            details="no scripts/**/*.sh files",
            duration_s=0.0,
        )

    rel_files = [str(path.relative_to(repo_root)) for path in shell_files]
    return _run_external_check(
        name="shellcheck",
        command=["shellcheck", *rel_files],
        repo_root=repo_root,
        runner=runner,
    )


def check_tests(repo_root: Path, runner: Runner = run_command) -> CheckResult:
    return _run_external_check(
        name="tests",
        # Clear ARGS so `make preflight ARGS="--full"` does not leak into `make test`.
        command=["make", "test", "ARGS="],
        repo_root=repo_root,
        runner=runner,
    )


def run_preflight(
    repo_root: Path,
    *,
    full: bool,
    runner: Runner = run_command,
    yaml_loader: Optional[YamlLoader] = None,
) -> List[CheckResult]:
    results = [
        check_conflict_markers(repo_root, runner=runner),
        check_makefile(repo_root),
        check_packaging_boundary(repo_root),
        check_memory_boundary(repo_root),
        check_yaml_syntax(repo_root, loader=yaml_loader),
        check_ruff(repo_root, runner=runner),
        check_shellcheck(repo_root, runner=runner),
    ]

    if full:
        results.append(check_tests(repo_root, runner=runner))

    return results


def print_report(results: Sequence[CheckResult], *, full: bool) -> None:
    mode = "extended" if full else "fast"
    print(f"Preflight mode: {mode}")

    for result in results:
        print(f"[{result.status}] {result.name} ({result.duration_s:.2f}s)")
        if result.details and result.details != "ok":
            for line in result.details.splitlines():
                print(f"  {line}")

    failures = [result for result in results if not result.passed]
    if failures:
        print(f"\nPreflight FAILED ({len(failures)} check(s) failed).")
    else:
        print("\nPreflight PASSED.")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run unified preflight checks.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Repository root to validate (default: repo containing this script).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run extended mode (includes `make test`).",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    repo_root = Path(args.repo_root).resolve()
    if not repo_root.is_dir():
        print(f"Repository root not found: {repo_root}", file=sys.stderr)
        return 2

    results = run_preflight(repo_root, full=bool(args.full))
    print_report(results, full=bool(args.full))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
