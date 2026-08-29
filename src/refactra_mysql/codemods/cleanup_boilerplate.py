"""
LibCST Codemod: Remove connection/cursor boilerplate and clean up close() calls.

Handles ALL connection variable patterns (connection, conn) and ALL cursor patterns:

REMOVES:
  - connection = get_db_connection() / conn = get_db_connection()
  - cursor = connection.cursor(DictCursor) / cursor = conn.cursor(...)
  - with conn.cursor() as cursor:  (unwraps the with block body)
  - cursor.close()
  - connection.close() / conn.close()

CONVERTS:
  - connection.commit() / conn.commit() → db.commit()
  - connection.rollback() / conn.rollback() → db.rollback()
"""
import logging

import libcst as cst
import libcst.matchers as m
from libcst.codemod import VisitorBasedCodemodCommand, CodemodContext

logger = logging.getLogger("codemods.boilerplate")

# All known connection variable names
_CONN_NAMES = ("connection", "conn")


class CleanupBoilerplateCommand(VisitorBasedCodemodCommand):
    DESCRIPTION = "Removes raw connection/cursor boilerplate code."

    changes: list

    def __init__(self, context: CodemodContext) -> None:
        super().__init__(context)
        self.changes = []

    # -------------------------------------------------------------------------
    # Simple statement removals
    # -------------------------------------------------------------------------

    def leave_SimpleStatementLine(
        self,
        original_node: cst.SimpleStatementLine,
        updated_node: cst.SimpleStatementLine,
    ) -> cst.SimpleStatementLine | cst.RemovalSentinel:

        for stmt in updated_node.body:
            # REMOVE: connection/conn = get_db_connection()
            if isinstance(stmt, cst.Assign) and isinstance(stmt.value, cst.Call):
                if m.matches(stmt.value, m.Call(func=m.Name("get_db_connection"))):
                    var = self._get_assign_target_name(stmt)
                    self.changes.append(f"REMOVE  │ {var} = get_db_connection()")
                    return cst.RemovalSentinel.REMOVE

            # REMOVE: cursor = connection.cursor(...) / cursor = conn.cursor(...)
            if isinstance(stmt, cst.Assign) and isinstance(stmt.value, cst.Call):
                if isinstance(stmt.value.func, cst.Attribute):
                    if (
                        isinstance(stmt.value.func.attr, cst.Name)
                        and stmt.value.func.attr.value == "cursor"
                        and isinstance(stmt.value.func.value, cst.Name)
                        and stmt.value.func.value.value in _CONN_NAMES
                    ):
                        self.changes.append(f"REMOVE  │ cursor = {stmt.value.func.value.value}.cursor(...)")
                        return cst.RemovalSentinel.REMOVE

            # REMOVE: cursor.close()
            if self._is_method_call(stmt, "cursor", "close"):
                self.changes.append("REMOVE  │ cursor.close()")
                return cst.RemovalSentinel.REMOVE

            # REMOVE: connection.close() / conn.close()
            if isinstance(stmt, cst.Expr) and isinstance(stmt.value, cst.Call):
                for conn_name in _CONN_NAMES:
                    if self._is_method_call(stmt, conn_name, "close"):
                        self.changes.append(f"REMOVE  │ {conn_name}.close()")
                        return cst.RemovalSentinel.REMOVE

        return updated_node

    # -------------------------------------------------------------------------
    # With statement: unwrap `with conn.cursor() as cursor:` blocks
    # -------------------------------------------------------------------------

    def leave_With(
        self,
        original_node: cst.With,
        updated_node: cst.With,
    ) -> cst.With | cst.FlattenSentinel:
        """Unwrap: with conn.cursor() as cursor: → just the body."""

        if len(updated_node.items) != 1:
            return updated_node

        item = updated_node.items[0]
        call = item.item

        # Match: with conn.cursor(...) as cursor:
        if isinstance(call, cst.Call) and isinstance(call.func, cst.Attribute):
            if (
                isinstance(call.func.value, cst.Name)
                and call.func.value.value in _CONN_NAMES
                and isinstance(call.func.attr, cst.Name)
                and call.func.attr.value == "cursor"
            ):
                conn_name = call.func.value.value
                self.changes.append(f"UNWRAP  │ with {conn_name}.cursor() as cursor:")

                # Return the body statements directly (unwrap the with block)
                body = updated_node.body
                if isinstance(body, cst.IndentedBlock):
                    return cst.FlattenSentinel(body.body)

        return updated_node

    # -------------------------------------------------------------------------
    # Call conversions: commit/rollback
    # -------------------------------------------------------------------------

    def leave_Call(
        self, original_node: cst.Call, updated_node: cst.Call
    ) -> cst.Call:

        for conn_name in _CONN_NAMES:
            # connection/conn.commit() → db.commit()
            if m.matches(
                updated_node,
                m.Call(func=m.Attribute(value=m.Name(conn_name), attr=m.Name("commit"))),
            ):
                self.changes.append(f"CONVERT │ {conn_name}.commit() → db.commit()")
                return updated_node.with_changes(
                    func=cst.Attribute(value=cst.Name("db"), attr=cst.Name("commit"))
                )

            # connection/conn.rollback() → db.rollback()
            if m.matches(
                updated_node,
                m.Call(func=m.Attribute(value=m.Name(conn_name), attr=m.Name("rollback"))),
            ):
                self.changes.append(f"CONVERT │ {conn_name}.rollback() → db.rollback()")
                return updated_node.with_changes(
                    func=cst.Attribute(value=cst.Name("db"), attr=cst.Name("rollback"))
                )

        return updated_node

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _get_assign_target_name(stmt: cst.Assign) -> str:
        """Extract variable name from assignment target."""
        if stmt.targets and isinstance(stmt.targets[0].target, cst.Name):
            return stmt.targets[0].target.value
        return "?"

    @staticmethod
    def _is_method_call(stmt, obj_name: str, method_name: str) -> bool:
        """Check if statement is obj_name.method_name()."""
        if isinstance(stmt, cst.Expr) and isinstance(stmt.value, cst.Call):
            call = stmt.value
            if (
                isinstance(call.func, cst.Attribute)
                and isinstance(call.func.value, cst.Name)
                and call.func.value.value == obj_name
                and isinstance(call.func.attr, cst.Name)
                and call.func.attr.value == method_name
            ):
                return True
        return False
