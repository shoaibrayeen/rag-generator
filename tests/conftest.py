import os
import sys
from pathlib import Path

# Isolate test data from the real data/ directory before settings are imported.
os.environ.setdefault("DATA_DIR", str(Path(__file__).parent / "_data"))
os.environ.setdefault("LLM_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
