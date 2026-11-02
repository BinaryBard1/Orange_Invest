#!/usr/bin/env python3
"""CLI evaluation runner for Orange Invest Founding AI Engineer Errands Agent.

Executes all 17 automated evaluation scenarios, verifies all invariants,
prints the generated Eval Table to stdout, and saves it to eval_results.md.
"""

import sys
from tests.test_evals import ErrandsEvalSuite


def main():
    print("=" * 80)
    print("ORANGE INVEST - FOUNDING AI ENGINEER ASSIGNMENT EVALUATION SUITE")
    print("=" * 80)
    print("Running 17 automated scenarios with scripted simulated users...\n")

    suite = ErrandsEvalSuite(trace_dir="traces")
    results = suite.run_all()
    table_md = suite.generate_eval_table_markdown()

    print(table_md)

    # Save to eval_results.md
    with open("eval_results.md", "w", encoding="utf-8") as f:
        f.write(table_md)

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    print(f"\nSaved eval table to eval_results.md ({passed}/{total} scenarios passed)")

    if passed < total:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()

