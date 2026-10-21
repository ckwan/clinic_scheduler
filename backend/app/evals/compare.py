"""Compare two saved runs:  python -m app.evals.compare evals/results/a.json evals/results/b.json"""

import json
import sys
from pathlib import Path

from app.evals.report import compare


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m app.evals.compare BEFORE.json AFTER.json", file=sys.stderr)
        return 2
    before, after = (json.loads(Path(p).read_text()) for p in argv)
    print(compare(before, after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
