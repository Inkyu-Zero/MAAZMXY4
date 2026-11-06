"""Compatibility command for rebuilding the four shared bounty profiles."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
from build_script_tasks import build_bounties as build
if __name__=='__main__':build()
