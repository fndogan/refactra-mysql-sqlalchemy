"""
Refactra: MySQL to SQLAlchemy.

Run as `refactra-mysql` after installation or as `python -m refactra_mysql`
from a source checkout.

Usage:
    refactra-mysql analyze         — scan source code for SQL patterns
    refactra-mysql codemods        — run deterministic LibCST transforms
    refactra-mysql convert         — convert eligible SQL functions to ORM
    refactra-mysql convert-dynamic — process selected dynamic SQL functions
    refactra-mysql post-process    — preview or apply generated-code cleanup
    refactra-mysql syntax          — compile converted Python files
    refactra-mysql validate        — validate imports and model references
    refactra-mysql models          — validate SQLAlchemy model definitions
    refactra-mysql n1              — detect potential N+1 query patterns
    refactra-mysql consistency     — check cross-file call signatures
    refactra-mysql fix-consistency — repair selected stale call signatures
    refactra-mysql quality         — generate static quality metrics
    refactra-mysql coverage        — compare function conversion coverage
    refactra-mysql compare         — generate before-and-after comparisons
    refactra-mysql semantic        — generate semantic-equivalence scaffolds
    refactra-mysql benchmark       — generate performance-test scaffolds
"""

import sys


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    if command in {"-h", "--help"}:
        print(__doc__)
        return
    if command == "--version":
        from refactra_mysql import __version__

        print(__version__)
        return

    # Remove the subcommand so each module sees clean argv
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    if command == "analyze":
        from refactra_mysql.analyze import scanner
        scanner.main()
    elif command == "codemods":
        from refactra_mysql.codemods import runner
        runner.main()
    elif command == "convert":
        from refactra_mysql.converter import engine
        engine.main()
    elif command == "n1":
        from refactra_mysql.quality import n1_detector
        n1_detector.main()
    elif command == "validate":
        from refactra_mysql.quality import validator
        validator.main()
    elif command == "quality":
        from refactra_mysql.quality import code_quality
        code_quality.main()
    elif command == "coverage":
        from refactra_mysql.quality import function_coverage
        function_coverage.main()
    elif command == "consistency":
        from refactra_mysql.quality import call_consistency
        call_consistency.main()
    elif command == "fix-consistency":
        from refactra_mysql.codemods import call_consistency_fixer
        call_consistency_fixer.main()
    elif command == "benchmark":
        from refactra_mysql.quality import perf_benchmark
        perf_benchmark.main()
    elif command == "syntax":
        from refactra_mysql.quality import syntax_check
        syntax_check.main()
    elif command == "models":
        from refactra_mysql.quality import model_validation
        model_validation.main()
    elif command == "compare":
        from refactra_mysql.quality import comparison
        comparison.main()
    elif command == "semantic":
        from refactra_mysql.quality import semantic_validator
        semantic_validator.main()
    elif command == "convert-dynamic":
        from refactra_mysql.converter import dynamic_engine
        dynamic_engine.main()
    elif command == "post-process":
        from refactra_mysql.converter import post_process
        post_process.main()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
