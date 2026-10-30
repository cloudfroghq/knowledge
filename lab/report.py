"""The PASS/FAIL table the lab checks print, shared by ``check.py`` and ``check_clients.py``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Row:
    name: str
    ok: bool
    detail: str


def print_table(title: str, results: list[Row]) -> None:
    width = max(len(r.name) for r in results)
    print(f"\n{title}")
    print(f"{'CHECK'.ljust(width)}  RESULT  DETAIL")
    print(f"{'-' * width}  ------  ------")
    for r in results:
        print(f"{r.name.ljust(width)}  {'PASS' if r.ok else 'FAIL':<6}  {r.detail}")
    passed = sum(r.ok for r in results)
    print(f"\n{passed}/{len(results)} checks passed")
