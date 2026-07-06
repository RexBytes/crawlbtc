#!/usr/bin/env python3

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import subprocess
import re

# Load environment variables
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"

def list_scripts():
    scripts = []
    for f in sorted(SCRIPTS_DIR.glob("*.py")):
        match = re.match(r"(\d{2})_(.+)\.py", f.name)
        if match:
            step_num, name = match.groups()
            scripts.append((step_num, name.replace('_', '-'), str(f)))
    return scripts

def print_usage(scripts):
    print("\nUsage:")
    print("  ./main.py <script-name> | run-all\n")
    print("Available scripts:")
    for _, name, _ in scripts:
        print(f"  {name}")
    print("\nSpecial:")
    print("  run-all     Run all enabled scripts in sequence\n")

def run_script(script_path):
    print(f"\n▶️  Running: {script_path}\n")
    subprocess.run(["python3", script_path], check=False)

def main():
    scripts = list_scripts()

    if len(sys.argv) != 2:
        print_usage(scripts)
        return

    command = sys.argv[1]

    if command == "run-all":
        for _, _, path in scripts:
            run_script(path)
    else:
        match = next((s for s in scripts if s[1] == command), None)
        if match:
            run_script(match[2])
        else:
            print(f"❌ Unknown script: {command}")
            print_usage(scripts)

if __name__ == "__main__":
    main()



