# -*- coding: utf-8 -*-
"""Headless compression pass, sharing state.json with the panel.

Runs the same do_optimize() the panel's button runs, so progress and results
show up in the panel afterwards. Safe to stop with Ctrl+C and re-run: each
finished file is recorded and skipped next time. Originals are never touched.
"""
import io, os, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import panel, library

panel.load_state()
panel.rows = library.scan(panel.state["root"])
print("library : %s  (%d videos)" % (panel.state["root"], len(panel.rows)))
print("target  : %d MB per file" % panel.upload_ceiling())
print("output  : %s" % panel.state["optimized_dir"])
print("-" * 64)

panel.job.update(running=True, cancel=False)
try:
    panel.do_optimize()
except KeyboardInterrupt:
    print("\nstopped — rerun to continue where it left off")
finally:
    panel.job.update(running=False)
    panel.save_state()
