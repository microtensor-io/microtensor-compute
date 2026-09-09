from __future__ import annotations

import ast
import importlib
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("protocol", "agent", "validator")


def modules() -> list[str]:
    found: list[str] = []
    for package in PACKAGES:
        found.append(package)
        for info in pkgutil.walk_packages([str(ROOT / package)], prefix=f"{package}."):
            found.append(info.name)
    return found


def local_names(module: str, tree: ast.Module) -> list[tuple[str, str | None]]:
    names: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0] in PACKAGES:
                for alias in node.names:
                    names.append((node.module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in PACKAGES:
                    names.append((alias.name, None))
    return names


def main() -> int:
    sys.path.insert(0, str(ROOT))
    failures: list[str] = []
    for name in modules():
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            failures.append(f"{name}: import failed: {exc}")
            continue
        path = getattr(module, "__file__", None)
        if not path:
            continue
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for target, symbol in local_names(name, tree):
            try:
                loaded = importlib.import_module(target)
            except Exception as exc:
                failures.append(f"{name}: {target} does not import: {exc}")
                continue
            if symbol and symbol != "*" and not hasattr(loaded, symbol):
                try:
                    importlib.import_module(f"{target}.{symbol}")
                except Exception:
                    failures.append(f"{name}: {target}.{symbol} does not exist")
    for line in failures:
        print(line)
    print(f"scanned {len(modules())} modules, {len(failures)} problems")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
