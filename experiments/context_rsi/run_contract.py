"""Zero-provider entry for the production CodingAgent RSI closed-loop contract.

Run from the project environment. Output must be new; generated workspaces and
sessions are local evidence, not model-quality or production-promotion proof.
"""
import argparse
import asyncio
import json
from pathlib import Path

from my_coding_agent.rsi_runtime import run_contract_demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run_contract_demo(args.output))
    print(json.dumps({"output": str(args.output.resolve()), "actual_provider_calls": result["actual_provider_calls"],
                      "quality": result["quality"], "optimizer_promoted": result["optimizer"]["promoted"]}))


if __name__ == "__main__":
    main()
