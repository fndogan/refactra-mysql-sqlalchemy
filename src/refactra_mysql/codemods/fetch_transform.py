"""
LibCST Codemod: Transform fetch calls to SQLAlchemy result proxy pattern.

Converts:
- cursor.fetchall() → _result.mappings().all()
- cursor.fetchone() → _result.mappings().first()
- cursor.lastrowid → _result.lastrowid
- cursor.rowcount → _result.rowcount
"""
import logging

import libcst as cst
import libcst.matchers as m
from libcst.codemod import VisitorBasedCodemodCommand, CodemodContext

logger = logging.getLogger("codemods.fetch_transform")


class FetchTransformCommand(VisitorBasedCodemodCommand):
    DESCRIPTION = "Converts cursor.fetchall()/fetchone() to result.mappings() pattern."

    changes: list

    def __init__(self, context: CodemodContext) -> None:
        super().__init__(context)
        self.changes = []

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        """Transform fetch calls."""

        # cursor.fetchall() → _result.mappings().all()
        if m.matches(updated_node, m.Call(func=m.Attribute(value=m.Name("cursor"), attr=m.Name("fetchall")))):
            self.changes.append("CONVERT │ cursor.fetchall() → _result.mappings().all()")
            return cst.Call(
                func=cst.Attribute(
                    value=cst.Call(
                        func=cst.Attribute(value=cst.Name("_result"), attr=cst.Name("mappings")),
                    ),
                    attr=cst.Name("all"),
                ),
            )

        # cursor.fetchone() → _result.mappings().first()
        if m.matches(updated_node, m.Call(func=m.Attribute(value=m.Name("cursor"), attr=m.Name("fetchone")))):
            self.changes.append("CONVERT │ cursor.fetchone() → _result.mappings().first()")
            return cst.Call(
                func=cst.Attribute(
                    value=cst.Call(
                        func=cst.Attribute(value=cst.Name("_result"), attr=cst.Name("mappings")),
                    ),
                    attr=cst.Name("first"),
                ),
            )

        return updated_node

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.Attribute:
        """Transform cursor.lastrowid/rowcount."""

        if m.matches(updated_node, m.Attribute(value=m.Name("cursor"), attr=m.Name("lastrowid"))):
            self.changes.append("CONVERT │ cursor.lastrowid → _result.lastrowid")
            return updated_node.with_changes(value=cst.Name("_result"))

        if m.matches(updated_node, m.Attribute(value=m.Name("cursor"), attr=m.Name("rowcount"))):
            self.changes.append("CONVERT │ cursor.rowcount → _result.rowcount")
            return updated_node.with_changes(value=cst.Name("_result"))

        return updated_node
