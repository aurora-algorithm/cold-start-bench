#!/usr/bin/env python3
"""Runs a command pinned to a set of logical CPUs; on Windows the command's own children inherit the mask.

usage: python pin.py CPUS COMMAND...      CPUS like "4", "0,2,4,6" or "8-15"
"""
import subprocess
import sys

import psutil


def cpus(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        out += list(range(int(lo), int(hi or lo) + 1))
    return out


def main() -> int:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    psutil.Process().cpu_affinity(cpus(sys.argv[1]))
    return subprocess.call(sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
