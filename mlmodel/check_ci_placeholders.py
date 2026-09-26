import os
import sys

def verify_no_placeholder_data():
    """
    CI / Startup Guardrail Check:
    Fails build/startup if any synthetic placeholder data generator,
    flat fallback mock confidence, or default demo data seed is reachable in the main pipeline.
    """
    python_files = []
    for root, dirs, files in os.walk('.'):
        if 'node_modules' in root or '.git' in root or '__pycache__' in root or 'tests' in root or 'check_ci' in root:
            continue
        for f in files:
            if f.endswith('.py') and f != 'check_ci_placeholders.py':
                python_files.append(os.path.join(root, f))

    violations = []
    forbidden_terms = [
        'SYNTHETIC_' + 'DATA',
        'PLACEHOLDER_' + 'TRANSACTIONS',
        'MOCK_' + 'CLASSIFICATION',
        'DEFAULT_DEMO_' + 'SEED'
    ]

    for p in python_files:
        with open(p, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            for term in forbidden_terms:
                if term in content:
                    violations.append(f"Forbidden placeholder term '{term}' found in {p}")

    if violations:
        print("[FAIL] CI STARTUP CHECK FAILED: Placeholder data detected in production code path!")
        for v in violations:
            print("  -", v)
        sys.exit(1)

    print("[PASS] CI STARTUP CHECK PASSED: Zero synthetic placeholder data in production pipeline.")

if __name__ == '__main__':
    verify_no_placeholder_data()
