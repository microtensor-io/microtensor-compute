from __future__ import annotations

import argparse
import sys
from pathlib import Path

MASK64 = (1 << 64) - 1
MASK32 = (1 << 32) - 1
GOLDEN = 0x9E3779B97F4A7C15
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
CASES = ((1, 2, 64, 2), (0xDEADBEEF, 0x1234, 96, 3), (7, 11, 128, 1))


def mix64(z: int) -> int:
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31)


def seed_state(seed: int, cipher: int) -> int:
    return (mix64(seed ^ 0xA5A5A5A5A5A5A5A5) + mix64((cipher * GOLDEN) & MASK64)) & MASK64


def element(state: int, index: int) -> int:
    return mix64((state + ((index + 1) * GOLDEN)) & MASK64) & MASK32


def fnv_u32(hash_value: int, value: int) -> int:
    for i in range(4):
        hash_value ^= (value >> (8 * i)) & 0xFF
        hash_value = (hash_value * FNV_PRIME) & MASK64
    return hash_value


def fnv_u64(hash_value: int, value: int) -> int:
    for i in range(8):
        hash_value ^= (value >> (8 * i)) & 0xFF
        hash_value = (hash_value * FNV_PRIME) & MASK64
    return hash_value


def digest(seed: int, cipher: int, n: int, rounds: int) -> int:
    state = seed_state(seed, cipher)
    try:
        import numpy as np

        matrix = np.fromiter((element(state, k) for k in range(n * n)), dtype=np.uint32, count=n * n)
        matrix = matrix.reshape(n, n)
        for _ in range(rounds):
            matrix = (matrix.astype(np.uint64) @ matrix.astype(np.uint64)).astype(np.uint32)
        rows = [int(v) for v in matrix.flatten().tolist()]
    except ImportError:
        rows = [element(state, k) for k in range(n * n)]
        for _ in range(rounds):
            out = [0] * (n * n)
            for i in range(n):
                row = rows[i * n : (i + 1) * n]
                for j in range(n):
                    acc = 0
                    for k in range(n):
                        acc += row[k] * rows[k * n + j]
                    out[i * n + j] = acc & MASK32
            rows = out
    folded = FNV_OFFSET
    for i in range(n):
        h = FNV_OFFSET
        for j in range(n):
            h = fnv_u32(h, rows[i * n + j])
        folded = fnv_u64(folded, h)
    folded = fnv_u64(folded, seed)
    folded = fnv_u64(folded, cipher)
    folded = fnv_u32(folded, n)
    folded = fnv_u32(folded, rounds)
    return folded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", type=Path, default=None)
    args = parser.parse_args()
    expected = {case: digest(*case) for case in CASES}
    for case, value in expected.items():
        print(f"{case[0]} {case[1]} {case[2]} {case[3]} {value:016x}")
    if args.compare is None:
        return 0
    seen = 0
    for line in args.compare.read_text().splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        case = tuple(int(p) for p in parts[:4])
        seen += 1
        if case not in expected:
            print(f"unexpected case {case}")
            return 1
        if expected[case] != int(parts[4], 16):
            print(f"mismatch for {case}: python {expected[case]:016x} c {parts[4]}")
            return 1
    if seen != len(CASES):
        print(f"expected {len(CASES)} cases, saw {seen}")
        return 1
    print("challenge reference agrees")
    return 0


if __name__ == "__main__":
    sys.exit(main())
