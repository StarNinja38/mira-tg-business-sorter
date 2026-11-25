# -*- coding: utf-8 -*-
"""Надёжный smoke-раннер (программный unittest), чтобы не виснуть на `unittest|tail` по ssh.
Залить на сервер в /tmp/run_smoke.py и запускать: ./venv/bin/python -u /tmp/run_smoke.py"""
import os, sys, unittest
os.chdir("/opt/tg-business-sorter"); sys.path.insert(0, "/opt/tg-business-sorter")
res = unittest.TextTestRunner(verbosity=0).run(
    unittest.TestLoader().loadTestsFromName("tests.test_core"))
print(f"SMOKE: ran={res.testsRun} failures={len(res.failures)} "
      f"errors={len(res.errors)} ok={res.wasSuccessful()}", flush=True)
