#!/usr/bin/env python3
"""run_detached.py — spawn a command fully detached (new session, own log).

Usage: run_detached.py <logfile> <cmd...>
"""
import subprocess
import sys

logf = open(sys.argv[1], "a")
cmd = sys.argv[2:]
p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf,
                     stderr=subprocess.STDOUT, start_new_session=True)
print("pid:", p.pid)
