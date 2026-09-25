#!/usr/bin/env python3
"""Run one agent tool as the unprivileged 'agent' user.

The web server (user agentd) calls this through sudo:
    sudo -n -u agent -H /opt/agent/venv/bin/python /opt/agent/toolrunner.py <tool>
with {"arg": ..., "content": ...} as JSON on stdin. The result goes to stdout.
"""
import json
import sys

import agent as core


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in core.TOOLS:
        print("Error: unknown tool")
        sys.exit(2)
    try:
        req = json.load(sys.stdin)
    except ValueError:
        print("Error: bad request")
        sys.exit(2)
    print(core.TOOLS[sys.argv[1]](req.get("arg", ""), req.get("content", "")), end="")


if __name__ == "__main__":
    main()
