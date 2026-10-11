"""Command-line interface for jira-cli.

Three modules live here:

* :mod:`jira_cli.cli.main` -- the non-interactive command tree and
  ``main(argv=None) -> int``, which is the ``jira`` console script.
* :mod:`jira_cli.cli.output` -- width-aware table/report rendering, issue
  printing, colour and the ``Proceed?`` confirmation.
* :mod:`jira_cli.cli.menu` -- the interactive menu, used when ``jira`` is run
  with no subcommand.

Nothing is imported here on purpose: importing this package must stay free of
side effects and free of cost, so ``jira mcp`` and the interactive menu are
loaded only when they are actually used.  Import the entry point explicitly::

    from jira_cli.cli.main import main

Deliberately absent: there is no ``jira_cli/__main__.py``.  The supported
entry points are the ``jira`` console script and ``jira mcp``.
"""

#: Empty on purpose -- see the module docstring; import submodules explicitly.
__all__: list[str] = []

