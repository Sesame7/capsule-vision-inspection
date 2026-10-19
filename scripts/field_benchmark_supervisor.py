"""Private detached process entry; no independent trigger-plan admission."""

import sys
from pathlib import Path

from field_benchmark.hosting import supervisor_entry

if __name__ == "__main__":
    supervisor_entry(Path(sys.argv[1]))
