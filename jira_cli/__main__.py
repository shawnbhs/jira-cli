"""Allow the package to be executed with ``python -m jira_cli``.

Equivalent to the ``jira`` console script installed by ``pip install -e .``.
"""

from __future__ import annotations

import sys

from jira_cli.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
