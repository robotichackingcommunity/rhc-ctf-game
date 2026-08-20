#!/usr/bin/env python3
"""Offline flag checker for the RoboHack AI CTF (public release).

Usage:
    python check_flag.py RHC{...}

Compares the SHA-256 hash of the submitted flag against a bundled hash list
(Q1-Q5; Q6/Q7 were badge-exclusive and are not included in this release).
No network access, no server dependency.
"""
import hashlib
import sys

HASHES = {
    "Q1": "09bbfbce0f94780d22672344d8eff19243f14f8f47bddb39958e638d2823c860",
    "Q2": "7d0bd19455ad35dc60635199125ab94e9ec9ec415be0f6103eb34403132265b8",
    "Q3": "edb0a8782f2c3f90ca9ea517d398fe3490f50499b8e4e2823b83e469b7881deb",
    "Q4": "69cb9fbb1e4945d6b9e85a426f17c8804681b89ebfdb2fc6312089620e54c9a3",
    "Q5": "ff33a85434ef9a2baf16f7f1f859dea50b5c8a2ffbbebabf0ea2e95923aede00",
}


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python check_flag.py RHC{...}")
        return 1

    flag = sys.argv[1]
    digest = hashlib.sha256(flag.encode()).hexdigest()

    for challenge, expected in HASHES.items():
        if digest == expected:
            print(f"Correct! That's the flag for {challenge}.")
            return 0

    print("Incorrect.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
