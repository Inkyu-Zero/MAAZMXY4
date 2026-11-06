"""Rebuild shared round profiles from resource definitions, never from old chains."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
from build_script_tasks import build as configure
if __name__=='__main__':configure()
