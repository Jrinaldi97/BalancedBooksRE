"""SQLite integrity barriers for coordinated posted journals."""


def install(connection):
    from sqlalchemy import inspect

    statements = [
        """CREATE TRIGGER IF NOT EXISTS re_allocation_replace BEFORE INSERT ON re_line_allocations
        WHEN NEW.id IS NOT NULL AND EXISTS(SELECT 1 FROM re_line_allocations WHERE id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'Posted portfolio history is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS re_seal_journal BEFORE INSERT ON re_journal_scopes BEGIN
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM transaction_lines WHERE transaction_id=NEW.transaction_id)
        OR (SELECT SUM(CAST(ROUND(debit*100) AS INTEGER)-CAST(ROUND(credit*100) AS INTEGER)) FROM transaction_lines WHERE transaction_id=NEW.transaction_id) <> 0
        THEN RAISE(ABORT,'Portfolio journal is empty or unbalanced') END;
        SELECT CASE WHEN EXISTS (SELECT 1 FROM transaction_lines l LEFT JOIN re_account_scopes a ON a.account_id=l.account_id
        WHERE l.transaction_id=NEW.transaction_id AND (a.entity_id IS NULL OR a.entity_id<>NEW.entity_id))
        THEN RAISE(ABORT,'Portfolio journal contains another entity account') END;
        END""",
        """CREATE TRIGGER IF NOT EXISTS re_line_insert BEFORE INSERT ON transaction_lines
        WHEN EXISTS(SELECT 1 FROM re_journal_scopes WHERE transaction_id=NEW.transaction_id)
        BEGIN SELECT RAISE(ABORT,'Posted portfolio journal is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS re_account_identity BEFORE UPDATE OF account_type,bank_kind,parent_id ON accounts
        WHEN EXISTS(SELECT 1 FROM re_account_scopes WHERE account_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Portfolio account identity is immutable'); END""",
    ]
    for table, condition in [
        (
            "transaction_lines",
            "EXISTS(SELECT 1 FROM re_journal_scopes WHERE transaction_id=OLD.transaction_id)",
        ),
        (
            "transactions",
            "EXISTS(SELECT 1 FROM re_journal_scopes WHERE transaction_id=OLD.id)",
        ),
        ("re_events", "1"),
        ("re_reporting_groups", "1"),
        ("re_group_members", "1"),
        ("re_group_adjustments", "1"),
        ("re_reporting_policies", "1"),
        ("re_cash_classifications", "1"),
        ("re_line_allocations", "1"),
        ("re_schedule_postings", "1"),
        ("re_schedules", "1"),
        ("re_reconciliations", "1"),
        ("re_reconciliation_revisions", "1"),
        ("re_journal_scopes", "1"),
        ("re_line_scopes", "1"),
        ("re_account_scopes", "1"),
        ("re_basis_adjustments", "1"),
        ("re_management_allocations", "1"),
        ("re_item_applications", "1"),
        ("re_document_locations", "1"),
        ("re_document_links", "1"),
        ("re_requests", "1"),
        ("re_report_runs", "1"),
    ]:
        # Earlier migrations also invoke install before the revision table exists.
        if table == "re_reconciliation_revisions" and not inspect(connection).has_table(
            table
        ):
            continue
        for operation in ("UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS re_guard_{table}_{operation} BEFORE {operation} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'Posted portfolio history is immutable'); END"
            )
    for sql in statements:
        connection.exec_driver_sql(sql)
    if inspect(connection).has_table("re_reconciliation_revisions"):
        connection.exec_driver_sql(
            """CREATE TRIGGER IF NOT EXISTS re_revision_replace
        BEFORE INSERT ON re_reconciliation_revisions
        WHEN EXISTS(SELECT 1 FROM re_reconciliation_revisions
          WHERE id=NEW.id OR supersedes_id=NEW.supersedes_id OR replacement_id=NEW.replacement_id)
        BEGIN SELECT RAISE(ABORT,'Reviewed reconciliation history is immutable'); END"""
        )
    from app.portfolio.operation_models import install_guards

    install_guards(connection)
    from app.portfolio.card_models import install_card_guards

    install_card_guards(connection)
