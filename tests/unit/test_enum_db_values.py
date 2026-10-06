"""Guard: the ORM's enum bindings must match the PostgreSQL enum types.

The bug this prevents (it reached a real deployment): SQLAlchemy's
``Enum(SomeEnum)`` persists the **member name** by default, so
``ScrapeStage.LISTING`` was bound as ``'LISTING'`` while migration 0001
creates the type as ``scrape_stage('listing', 'article')``. Every insert died:

    psycopg2.errors.InvalidTextRepresentation:
        invalid input value for enum scrape_stage: "LISTING"

``articles.status`` had the same defect, and worse: ``ArticleStatus.PARTIAL``
maps to the value ``"partial_extraction"``, which no amount of upper-casing
would have fixed.

These checks are deliberately offline — no database needed — because the
existing suite passed while the schema was unusable.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import postgresql

from app.db.models import Base

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "app" / "db" / "migrations" / "versions"


def _migration_enum_labels() -> dict[str, list[str]]:
    """Map PostgreSQL enum type name -> labels, as declared in the migrations."""
    found: dict[str, list[str]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "Enum"):
                continue
            labels = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            name = next(
                (
                    kw.value.value
                    for kw in node.keywords
                    if kw.arg == "name"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ),
                None,
            )
            if name and labels:
                found[name] = labels
    return found


def _model_enum_columns() -> dict[str, SAEnum]:
    """Map PostgreSQL enum type name -> the column type used by the ORM."""
    columns: dict[str, SAEnum] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, SAEnum) and column.type.name:
                columns[column.type.name] = column.type
    return columns


MIGRATION_LABELS = _migration_enum_labels()
MODEL_TYPES = _model_enum_columns()


def test_migrations_declare_the_expected_enums() -> None:
    """Sanity: parsing the migrations actually found both enum types."""
    assert set(MIGRATION_LABELS) == {"article_status", "scrape_stage"}, MIGRATION_LABELS


def test_every_model_enum_type_has_a_migration() -> None:
    missing = set(MODEL_TYPES) - set(MIGRATION_LABELS)
    assert not missing, f"model enum types with no CREATE TYPE in migrations: {sorted(missing)}"


@pytest.mark.parametrize("type_name", sorted(MIGRATION_LABELS))
def test_model_enum_labels_match_the_migration(type_name: str) -> None:
    """The strings the ORM binds must equal the labels the DB type accepts."""
    assert type_name in MODEL_TYPES, f"{type_name} is missing from the models"
    assert sorted(MODEL_TYPES[type_name].enums) == sorted(MIGRATION_LABELS[type_name]), (
        f"{type_name}: the ORM would bind {MODEL_TYPES[type_name].enums} but the "
        f"migration creates {MIGRATION_LABELS[type_name]} — inserts would fail with "
        f'InvalidTextRepresentation: invalid input value for enum {type_name}'
    )


@pytest.mark.parametrize("type_name", sorted(MIGRATION_LABELS))
def test_enum_binds_values_not_member_names(type_name: str) -> None:
    """values_callable must be in use, i.e. bind the value ("partial_extraction")."""
    column_type = MODEL_TYPES[type_name]
    enum_cls = column_type.enum_class
    assert enum_cls is not None, f"{type_name} should be backed by a Python enum"

    values = {member.value for member in enum_cls}
    names = {member.name for member in enum_cls}
    assert set(column_type.enums) == values, (
        f"{type_name} binds member names instead of values — pass "
        f"values_callable=_enum_values to SAEnum()."
    )
    if values != names:  # e.g. PARTIAL -> "partial_extraction"
        assert not (set(column_type.enums) & (names - values)), (
            f"{type_name} leaks Python member names into the database"
        )


def test_compiled_insert_binds_the_lowercase_value() -> None:
    """End-to-end-ish: compile a real INSERT and inspect the literal."""
    from app.db.models import ScrapeRun, ScrapeStage

    stmt = (
        ScrapeRun.__table__.insert()
        .values(run_uuid="00000000-0000-0000-0000-000000000000:listing",
                stage=ScrapeStage.LISTING)
    )
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    assert "'listing'" in sql, f"expected the enum value in the INSERT, got:\n{sql}"
    assert "'LISTING'" not in sql, "the enum member name leaked into the INSERT"
