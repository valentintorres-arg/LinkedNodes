"""Entry point of LinkedNodes.exe.

Started plainly, or with a folder, it opens the desktop window. Started with one of the
command names it runs that command instead, without a window and without output, which
is how a machine with no Python can still scan and build from a script.
"""
import sys

from linkednodes.cli import main

COMMANDS = {"scan", "build", "refresh", "serve", "query", "path", "node", "changed", "hook", "install", "app"}

if __name__ == "__main__":
    given = sys.argv[1:]
    sys.exit(main(given if given and given[0] in COMMANDS else ["app", *given]))
