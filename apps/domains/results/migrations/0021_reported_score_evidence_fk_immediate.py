from django.db import migrations


def _evidence_fk_constraint(apps, schema_editor) -> tuple[str, str]:
    reported_score = apps.get_model("results", "StudentReportedScore")
    inventory_file = apps.get_model("inventory", "InventoryFile")
    table_name = reported_score._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor,
            table_name,
        )

    matches = [
        name
        for name, details in constraints.items()
        if details.get("columns") == ["evidence_file_id"]
        and details.get("foreign_key")
        == (inventory_file._meta.db_table, "id")
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Expected one StudentReportedScore evidence_file foreign key; "
            f"found {len(matches)}"
        )
    return table_name, matches[0]


def make_evidence_fk_immediate(apps, schema_editor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    table_name, constraint_name = _evidence_fk_constraint(apps, schema_editor)
    schema_editor.execute(
        "ALTER TABLE %s ALTER CONSTRAINT %s NOT DEFERRABLE"
        % (
            schema_editor.quote_name(table_name),
            schema_editor.quote_name(constraint_name),
        )
    )


def restore_evidence_fk_deferred(apps, schema_editor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    table_name, constraint_name = _evidence_fk_constraint(apps, schema_editor)
    schema_editor.execute(
        "ALTER TABLE %s ALTER CONSTRAINT %s DEFERRABLE INITIALLY DEFERRED"
        % (
            schema_editor.quote_name(table_name),
            schema_editor.quote_name(constraint_name),
        )
    )


class Migration(migrations.Migration):
    dependencies = [
        ("results", "0020_scoreeditdraft_client_id"),
    ]

    operations = [
        migrations.RunPython(
            make_evidence_fk_immediate,
            reverse_code=restore_evidence_fk_deferred,
        ),
    ]
