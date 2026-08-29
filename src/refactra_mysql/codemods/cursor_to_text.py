"""
LibCST Codemod: Transform cursor.execute() to db.execute(text()).

Converts cursor.execute("SQL", params) → db.execute(text("SQL"), params)
"""
import logging

import libcst as cst
import libcst.matchers as m
from libcst.codemod import VisitorBasedCodemodCommand, CodemodContext

logger = logging.getLogger("codemods.cursor_to_text")


class CursorToTextCommand(VisitorBasedCodemodCommand):
    DESCRIPTION = "Converts cursor.execute(sql, params) to db.execute(text(sql), params)."

    changes: list

    def __init__(self, context: CodemodContext) -> None:
        super().__init__(context)
        self.changes = []

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        """Transform cursor.execute() calls."""

        if not m.matches(updated_node, m.Call(func=m.Attribute(value=m.Name("cursor"), attr=m.Name("execute")))):
            return updated_node

        args = list(updated_node.args)
        if not args:
            return updated_node

        # Wrap SQL arg in text()
        sql_arg = args[0]
        wrapped_sql = cst.Call(
            func=cst.Name("text"),
            args=[cst.Arg(value=sql_arg.value)],
        )

        new_args = [cst.Arg(value=wrapped_sql)]
        if len(args) > 1:
            new_args.append(args[1].with_changes(keyword=None))

        self.changes.append("CONVERT │ cursor.execute(sql) → db.execute(text(sql))")

        return updated_node.with_changes(
            func=cst.Attribute(value=cst.Name("db"), attr=cst.Name("execute")),
            args=new_args,
        )
