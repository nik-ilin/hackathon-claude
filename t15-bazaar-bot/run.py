#!/usr/bin/env python3
"""Entry point for the Bazaar agent.

    BAZAAR_URL=https://bazaar.causaprima.ai BAZAAR_KEY=tk-xxxx-xxxx python3 run.py

Set LOG_LEVEL=DEBUG for verbose output.
"""
import sys
import os

# Ensure the project directory is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



def main():
    if "BAZAAR_KEY" not in os.environ:
        print("Error: set BAZAAR_KEY=tk-xxxx-xxxx in your environment.")
        print("  export BAZAAR_URL=https://bazaar.causaprima.ai")
        print("  export BAZAAR_KEY=tk-xxxx-xxxx")
        print("  python3 run.py")
        sys.exit(1)

    raise SystemExit("Ejecutor heredado desactivado en esta integración. Usa bazaar-kit/run.sh memory o python3 -m bazaar.agent --dry-run")


if __name__ == "__main__":
    main()
