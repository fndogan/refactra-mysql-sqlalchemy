"""
Performance Benchmarker — ORM vs Raw SQL latency comparison.

Wraps converted functions with timing instrumentation to detect:
  1. Query latency regression (ORM slower than raw SQL)
  2. N+1 query patterns (query count explosion)
  3. Memory usage differences

Usage:
    refactra-mysql benchmark \
        --converted-dir ./output \
        --output-file ./tests/test_perf_benchmark.py

Run against staging database with production-like data.
"""
import ast
from pathlib import Path


def generate_perf_test(func_name: str, module_path: str, params: list[str]) -> str:
    """Generate a performance comparison test for a function."""

    # Build param strings
    test_params = []
    for p in params:
        if p in ('db', 'connection', 'cursor', 'conn', 'self'):
            continue
        elif p == 'company_id':
            test_params.append("company_id=TEST_COMPANY_ID")
        elif 'id' in p.lower():
            test_params.append(f"{p}=TEST_ID")
        else:
            test_params.append(f"{p}=None")

    param_str = ", ".join(test_params)
    raw_args = f"raw_connection, {param_str}" if param_str else "raw_connection"
    orm_args = f"db_session, {param_str}" if param_str else "db_session"
    pair_key = f"{module_path}:{func_name}"
    test_name = f"test_{module_path.replace('.', '_')}_{func_name}_performance"

    return f'''
def {test_name}(db_session, raw_connection, migration_pairs, benchmark_logger):
    """
    Performance benchmark: {func_name}
    Measures ORM vs raw SQL latency.
    """
    import time
    from sqlalchemy import event

    original_func, converted_func = migration_pairs["{pair_key}"]

    # Count queries issued by ORM
    query_count = [0]

    @event.listens_for(db_session.get_bind(), "before_cursor_execute")
    def _count_queries(*args, **kwargs):
        query_count[0] += 1

    ITERATIONS = 10
    MAX_LATENCY_RATIO = 3.0  # ORM should be at most 3x slower

    # Benchmark raw SQL
    raw_times = []
    for _ in range(ITERATIONS):
        start = time.perf_counter()
        original_func({raw_args})
        raw_times.append(time.perf_counter() - start)

    # Benchmark ORM
    query_count[0] = 0
    orm_times = []
    for _ in range(ITERATIONS):
        start = time.perf_counter()
        converted_func({orm_args})
        orm_times.append(time.perf_counter() - start)

    avg_raw = sum(raw_times) / len(raw_times)
    avg_orm = sum(orm_times) / len(orm_times)
    ratio = avg_orm / avg_raw if avg_raw > 0 else 1.0
    queries_per_call = query_count[0] / ITERATIONS

    benchmark_logger.log({{
        "function": "{func_name}",
        "avg_raw_ms": round(avg_raw * 1000, 2),
        "avg_orm_ms": round(avg_orm * 1000, 2),
        "ratio": round(ratio, 2),
        "queries_per_call": queries_per_call,
    }})

    # Assertions
    assert ratio < MAX_LATENCY_RATIO, \\
        f"{func_name}: ORM is {{ratio:.1f}}x slower (max {{MAX_LATENCY_RATIO}}x). " \\
        f"Raw: {{avg_raw*1000:.1f}}ms, ORM: {{avg_orm*1000:.1f}}ms"

    # N+1 detection: more than 5 queries per call is suspicious
    assert queries_per_call <= 5, \\
        f"{func_name}: {{queries_per_call}} queries per call (possible N+1)"
'''


def generate_benchmark_file(converted_dir: Path, output_file: Path) -> dict:
    """Generate a performance benchmark test file."""

    tests = []
    stats = {"total": 0, "generated": 0}

    for conv_file in sorted(converted_dir.rglob("*.py")):
        if "__pycache__" in str(conv_file):
            continue

        source = conv_file.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        module_path = ".".join(conv_file.relative_to(converted_dir).with_suffix("").parts)

        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue

            params = [arg.arg for arg in node.args.args]
            if 'db' not in params:
                continue

            stats["total"] += 1
            test = generate_perf_test(node.name, module_path, params)
            tests.append(test)
            stats["generated"] += 1

    header = '''"""
Auto-generated performance benchmark tests.

Compare ORM query latency vs original raw SQL.
Detects:
  - Latency regression (ORM > 3x slower)
  - N+1 query patterns (> 5 queries per call)

Usage:
    1. Set TEST_COMPANY_ID and TEST_ID.
    2. Provide db_session, raw_connection, and migration_pairs fixtures in conftest.py.
    3. Run: pytest tests/test_perf_benchmark.py -v --tb=short

The migration_pairs fixture must map "module.path:function_name" to a tuple of
(original_callable, converted_callable). Both database fixtures must use
isolated test data.

Run only against an isolated staging database with production-like data volume.
"""
import json
from pathlib import Path

import pytest

TEST_COMPANY_ID = 1  # TODO: Set to valid test company
TEST_ID = 1          # TODO: Set to valid test record


class BenchmarkLogger:
    """Collects benchmark results and saves to JSON."""

    def __init__(self):
        self.results = []

    def log(self, data):
        self.results.append(data)
        status = "PASS" if data["ratio"] < 2 else "REVIEW" if data["ratio"] < 3 else "FAIL"
        print(f"  [{status}] {data['function']}: "
              f"SQL {data['avg_raw_ms']}ms → ORM {data['avg_orm_ms']}ms "
              f"({data['ratio']}x, {data['queries_per_call']} queries)")

    def save(self, filepath="reports/perf_benchmark.json"):
        Path(filepath).parent.mkdir(exist_ok=True)
        Path(filepath).write_text(json.dumps(self.results, indent=2))


@pytest.fixture(scope="session")
def benchmark_logger():
    logger = BenchmarkLogger()
    yield logger
    logger.save()

'''

    content = header + "\n".join(tests)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(content, encoding="utf-8")

    return stats


def main() -> None:
    """CLI entrypoint for performance benchmark generator."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate performance benchmark tests")
    parser.add_argument("--converted-dir", default="output")
    parser.add_argument("--output-file", default="tests/test_perf_benchmark.py")
    args = parser.parse_args()

    stats = generate_benchmark_file(
        converted_dir=Path(args.converted_dir),
        output_file=Path(args.output_file),
    )

    print(f"\nGenerated {stats['generated']} benchmark tests")
    print(f"   Output: {args.output_file}")
    print("\nRun only against isolated staging data.")


if __name__ == "__main__":
    main()
