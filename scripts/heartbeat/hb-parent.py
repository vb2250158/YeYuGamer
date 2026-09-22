"""Identify parent PIDs recorded in game launch logs: alive? what name?"""
import csv
import io
import subprocess
import sys

out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
alive = {}
for row in csv.reader(io.StringIO(out)):
    if len(row) >= 2 and row[1].replace(",", "").isdigit():
        alive[int(row[1])] = row[0]

for raw in sys.argv[1:]:
    pid = int(raw)
    name = alive.get(pid)
    print("%-7s %s" % (pid, name if name else "<dead / not enumerated>"))
