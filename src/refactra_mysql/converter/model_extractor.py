"""
Model Extractor — Extracts relevant SQLAlchemy model classes for a given SQL function.

Parses a models file, indexes model classes by table name, and returns model
context relevant to the SQL supplied by the caller.

Usage:
    extractor = ModelExtractor("./path/to/models.py")
    context = extractor.get_context_for_sql("SELECT * FROM customers JOIN invoices ...")
    # Returns the matching model definitions.
"""
import ast
import re
from pathlib import Path
from typing import Optional

from refactra_mysql.config import setup_logging

logger = setup_logging("model_extractor")


class ModelExtractor:
    """
    Parses a SQLAlchemy models file and provides targeted model context
    for individual SQL functions.

    Attributes:
        models_by_table: Dict mapping table_name → model source code.
        models_by_class: Dict mapping ClassName → model source code.
        table_to_class: Dict mapping table_name → ClassName.
        class_to_table: Dict mapping ClassName → table_name.
        base_imports: Common imports needed for all model references.
    """

    def __init__(self, models_file: str | Path):
        """
        Initialize the extractor by parsing the models file.

        Args:
            models_file: Path to the SQLAlchemy models Python file.
        """
        self.models_file = Path(models_file)
        self.models_by_table: dict[str, str] = {}
        self.models_by_class: dict[str, str] = {}
        self.table_to_class: dict[str, str] = {}
        self.class_to_table: dict[str, str] = {}
        self.base_imports: str = ""

        self._parse()

    def _parse(self) -> None:
        """Parse the models file and index all model classes."""
        if not self.models_file.is_file():
            raise FileNotFoundError(f"Models file not found: {self.models_file}")

        source = self.models_file.read_text(encoding="utf-8")
        lines = source.splitlines()

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            raise ValueError(f"Cannot parse models file: {e}")

        # Extract imports section (everything before the first class)
        first_class_line = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if first_class_line is None or node.lineno < first_class_line:
                    first_class_line = node.lineno
                break

        if first_class_line:
            self.base_imports = "\n".join(lines[:first_class_line - 1]).strip()

        # Extract each class definition
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_name = node.name
                start = node.lineno - 1
                end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start + 1
                class_source = "\n".join(lines[start:end])

                # Extract __tablename__
                table_name = self._extract_tablename(node, class_source)

                if table_name:
                    self.models_by_table[table_name] = class_source
                    self.table_to_class[table_name] = class_name
                    self.class_to_table[class_name] = table_name

                self.models_by_class[class_name] = class_source

        logger.info(
            "Parsed %d model classes from %s",
            len(self.models_by_class),
            self.models_file.name,
        )

    @staticmethod
    def _extract_tablename(node: ast.ClassDef, source: str) -> Optional[str]:
        """Extract __tablename__ value from a class definition."""
        # Try AST extraction first
        for item in ast.walk(node):
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id == "__tablename__":
                        if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                            return item.value.value

        # Fallback to regex
        match = re.search(r"__tablename__\s*=\s*['\"](\w+)['\"]", source)
        if match:
            return match.group(1)

        return None

    def get_tables_from_sql(self, sql_or_code: str) -> set[str]:
        """
        Extract table names referenced in SQL or Python code.

        Detects tables from:
        - FROM table_name
        - JOIN table_name
        - INTO table_name
        - UPDATE table_name
        - Table aliases (FROM customers c)

        Args:
            sql_or_code: SQL string or Python code containing SQL.

        Returns:
            Set of detected table names.
        """
        found_tables = set()

        # Patterns that precede table names in SQL
        patterns = [
            r"\bFROM\s+(\w+)",
            r"\bJOIN\s+(\w+)",
            r"\bINTO\s+(\w+)",
            r"\bUPDATE\s+(\w+)",
            r"\bTABLE\s+(\w+)",
        ]

        for pattern in patterns:
            matches = re.findall(pattern, sql_or_code, re.IGNORECASE)
            for match in matches:
                table_lower = match.lower()
                # Verify it's a known table (not a SQL keyword)
                if table_lower in self.models_by_table:
                    found_tables.add(table_lower)

        # Also check if any known table name appears in the code
        # (catches cases like string concatenation or variables)
        for table_name in self.models_by_table:
            if table_name in sql_or_code.lower():
                found_tables.add(table_name)

        return found_tables

    def get_context_for_sql(self, sql_or_code: str, max_models: int = 10) -> str:
        """
        Get minimal model context relevant to a SQL function.

        Args:
            sql_or_code: SQL string or Python code containing SQL.
            max_models: Maximum number of models to include (safety limit).

        Returns:
            String containing only the relevant model class definitions
            with necessary imports.
        """
        tables = self.get_tables_from_sql(sql_or_code)

        if not tables:
            logger.debug("No tables detected in code, returning empty context")
            return ""

        # Collect relevant model sources
        model_sources = []
        included_count = 0

        for table_name in sorted(tables):
            if included_count >= max_models:
                logger.warning(
                    "Reached max_models limit (%d), skipping remaining tables",
                    max_models,
                )
                break

            if table_name in self.models_by_table:
                model_sources.append(self.models_by_table[table_name])
                class_name = self.table_to_class.get(table_name, "?")
                logger.debug("  Including model: %s (table: %s)", class_name, table_name)
                included_count += 1

        if not model_sources:
            return ""

        # Combine imports + relevant models
        context = self.base_imports + "\n\n" + "\n\n".join(model_sources)

        logger.debug(
            "Context: %d models, ~%d tokens (estimated)",
            len(model_sources),
            len(context) // 4,  # Rough token estimation
        )

        return context

    def get_context_for_file(self, filepath: str | Path) -> str:
        """
        Get model context for all SQL in a file.

        Scans the entire file content and returns models for all
        referenced tables.

        Args:
            filepath: Path to the Python file to analyze.

        Returns:
            Combined model context string.
        """
        source = Path(filepath).read_text(encoding="utf-8")
        return self.get_context_for_sql(source, max_models=20)

    def get_summary(self) -> dict:
        """Return a summary of parsed models."""
        return {
            "total_models": len(self.models_by_class),
            "total_tables": len(self.models_by_table),
            "tables": sorted(self.models_by_table.keys()),
            "classes": sorted(self.models_by_class.keys()),
        }
