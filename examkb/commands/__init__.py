"""One module per `examkb` subcommand.

`examkb/cli.py` owns the registry and the dispatch; a subcommand owns its
arguments and its body. Adding one is a module here plus an import in
`cli._load_subcommands()`, never an edit to the dispatch logic.
"""
