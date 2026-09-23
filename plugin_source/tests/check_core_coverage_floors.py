#!/usr/bin/env python3
"""Enforce per-file coverage floors on the core data-integrity path.

The global ``--cov-fail-under`` only protects the aggregate number; a single
file can regress without tripping it.  This script parses the ``coverage.xml``
that the CI coverage step already generates and fails if any core file drops
below its own floor.

Floors are set to the values actually measured when this was introduced —
never aspirational.  Future sessions may RAISE these as coverage improves,
but lowering one requires an explicit justification in the commit message.

Usage (after the coverage step):
    python tests/check_core_coverage_floors.py coverage.xml
"""

import sys
import xml.etree.ElementTree as ET

# Per-file coverage floors (percent, XML line-rate), measured from the
# session that introduced this check (2026-08-15, WSL coverage run after
# closing the data-integrity gaps). Raise, don't lower.
PER_FILE_FLOORS = {
    "crowd_anki/representation/deck.py": 60.0,  # measured 61.02
    "crowd_anki/representation/note.py": 70.0,  # measured 70.97
    "export_manager.py": 31.0,  # measured 31.58
    "import_manager.py": 43.0,  # measured 43.49
}


def parse_line_rates(xml_path: str):
    """Return {filename: line_rate} from a coverage.xml file."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    rates = {}
    for pkg in root.iter("package"):
        for cls in pkg.iter("class"):
            name = cls.get("filename")
            if name:
                rates[name] = float(cls.get("line-rate", 0.0)) * 100.0
    return rates


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python check_core_coverage_floors.py coverage.xml")
        return 2

    xml_path = sys.argv[1]
    try:
        rates = parse_line_rates(xml_path)
    except FileNotFoundError:
        print(f"error: coverage.xml not found at {xml_path}")
        return 1

    failures = []
    for filename, floor in PER_FILE_FLOORS.items():
        actual = rates.get(filename)
        if actual is None:
            print(f"WARN: {filename} not present in coverage.xml (excluded?)")
            continue
        if actual < floor:
            failures.append((filename, actual, floor))

    for filename, floor in PER_FILE_FLOORS.items():
        status = "ok" if filename not in [f[0] for f in failures] else "FAIL"
        print(
            f"{status:4}  {filename:45} {rates.get(filename, float('nan')):6.2f}%  (floor {floor}%)"
        )

    if failures:
        print("\nFAILURE: core data-integrity files below their coverage floors:")
        for filename, actual, floor in failures:
            print(f"  {filename}: {actual:.2f}% < floor {floor}%")
        print(
            "\nDo not lower a floor without an explicit justification in the "
            "commit message."
        )
        return 1

    print("\nAll core coverage floors met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
