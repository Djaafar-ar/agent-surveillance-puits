import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

collect_ignore = ["test_demo_page.py"]   # browser demo tests: run directly with python tests/test_demo_page.py
