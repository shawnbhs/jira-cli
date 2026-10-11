"""jira-cli — a read-first command line client for Jira Software Server.

The package deliberately keeps this module free of heavy imports so that
``import jira_cli`` stays cheap and never pulls in ``requests``.
"""

__version__ = "2.0.0"

__all__ = ["__version__"]
