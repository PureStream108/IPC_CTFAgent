"""deduplicate terminal goal intents and enforce one per project"""

from alembic import op


revision = "20260807_0002"
down_revision = "20260807_0001"
branch_labels = None
depends_on = None


# The migration intentionally uses fixed SQL instead of importing the mutable
# runtime schema contract. Existing installations must all execute the same
# cleanup before the partial unique index is created.
GOAL_INTENT_UNIQUENESS_UPGRADE_STATEMENTS = (
    "LOCK TABLE intents IN SHARE ROW EXCLUSIVE MODE",
    """
    WITH ranked AS (
        SELECT
            project_id,
            id,
            first_value(id) OVER (
                PARTITION BY project_id
                ORDER BY created_at NULLS LAST, id
            ) AS canonical_id,
            count(*) OVER (PARTITION BY project_id) AS goal_count
        FROM intents
        WHERE to_fact_id = 'goal'
    ),
    duplicate_groups AS (
        SELECT
            ranked.project_id,
            ranked.canonical_id,
            jsonb_agg(
                jsonb_build_object(
                    'intent', to_jsonb(intent_row),
                    'sources', COALESCE(
                        (
                            SELECT jsonb_agg(source.fact_id ORDER BY source.fact_id)
                            FROM intent_sources AS source
                            WHERE source.project_id = ranked.project_id
                              AND source.intent_id = ranked.id
                        ),
                        '[]'::jsonb
                    ),
                    'agent_links', COALESCE(
                        (
                            SELECT jsonb_agg(to_jsonb(link_row) ORDER BY link_row.id)
                            FROM agent_links AS link_row
                            WHERE link_row.project_id = ranked.project_id
                              AND (
                                  link_row.src = 'intent:' || ranked.id
                                  OR link_row.dst = 'intent:' || ranked.id
                              )
                        ),
                        '[]'::jsonb
                    ),
                    'report_ids', COALESCE(
                        (
                            SELECT jsonb_agg(report.id ORDER BY report.created_at, report.id)
                            FROM reports AS report
                            WHERE report.project_id = ranked.project_id
                              AND report.knowledge_json ? ('intent:' || ranked.id)
                        ),
                        '[]'::jsonb
                    )
                )
                ORDER BY intent_row.created_at NULLS LAST, intent_row.id
            ) FILTER (WHERE ranked.id <> ranked.canonical_id) AS duplicates
        FROM ranked
        JOIN intents AS intent_row
          ON intent_row.project_id = ranked.project_id
         AND intent_row.id = ranked.id
        WHERE ranked.goal_count > 1
        GROUP BY ranked.project_id, ranked.canonical_id
    )
    INSERT INTO audit_events (project_id, kind, payload)
    SELECT
        project_id,
        'migration.goal_intent_deduplicated',
        jsonb_build_object(
            'migration_revision', '20260807_0002',
            'canonical_intent_id', canonical_id,
            'duplicates', duplicates
        )
    FROM duplicate_groups
    WHERE jsonb_array_length(duplicates) > 0
    """,
    """
    WITH ranked AS (
        SELECT
            project_id,
            id,
            first_value(id) OVER (
                PARTITION BY project_id
                ORDER BY created_at NULLS LAST, id
            ) AS canonical_id,
            count(*) OVER (PARTITION BY project_id) AS goal_count
        FROM intents
        WHERE to_fact_id = 'goal'
    )
    UPDATE agent_links AS link
    SET src = CASE
                  WHEN link.src = 'intent:' || ranked.id
                  THEN 'intent:' || ranked.canonical_id
                  ELSE link.src
              END,
        dst = CASE
                  WHEN link.dst = 'intent:' || ranked.id
                  THEN 'intent:' || ranked.canonical_id
                  ELSE link.dst
              END
    FROM ranked
    WHERE ranked.goal_count > 1
      AND ranked.id <> ranked.canonical_id
      AND link.project_id = ranked.project_id
      AND (
          link.src = 'intent:' || ranked.id
          OR link.dst = 'intent:' || ranked.id
      )
    """,
    """
    WITH ranked AS (
        SELECT
            project_id,
            id,
            first_value(id) OVER (
                PARTITION BY project_id
                ORDER BY created_at NULLS LAST, id
            ) AS canonical_id,
            count(*) OVER (PARTITION BY project_id) AS goal_count
        FROM intents
        WHERE to_fact_id = 'goal'
    ),
    replacements AS (
        SELECT
            project_id,
            'intent:' || id AS duplicate_tag,
            'intent:' || canonical_id AS canonical_tag
        FROM ranked
        WHERE goal_count > 1 AND id <> canonical_id
    )
    UPDATE reports AS report
    SET knowledge_json = (
        SELECT COALESCE(jsonb_agg(item ORDER BY ordinal), '[]'::jsonb)
        FROM (
            SELECT DISTINCT ON (rewritten) rewritten AS item, ordinal
            FROM jsonb_array_elements_text(report.knowledge_json)
                 WITH ORDINALITY AS knowledge(item, ordinal)
            LEFT JOIN replacements AS replacement
              ON replacement.project_id = report.project_id
             AND replacement.duplicate_tag = knowledge.item
            CROSS JOIN LATERAL (
                SELECT COALESCE(replacement.canonical_tag, knowledge.item) AS rewritten
            ) AS normalized
            ORDER BY rewritten, ordinal
        ) AS deduplicated
    )
    WHERE EXISTS (
        SELECT 1
        FROM replacements AS replacement
        WHERE replacement.project_id = report.project_id
          AND report.knowledge_json ? replacement.duplicate_tag
    )
    """,
    """
    WITH ranked AS (
        SELECT
            project_id,
            id,
            first_value(id) OVER (
                PARTITION BY project_id
                ORDER BY created_at NULLS LAST, id
            ) AS canonical_id,
            count(*) OVER (PARTITION BY project_id) AS goal_count
        FROM intents
        WHERE to_fact_id = 'goal'
    )
    INSERT INTO intent_sources (intent_id, project_id, fact_id)
    SELECT ranked.canonical_id, source.project_id, source.fact_id
    FROM ranked
    JOIN intent_sources AS source
      ON source.project_id = ranked.project_id
     AND source.intent_id = ranked.id
    WHERE ranked.goal_count > 1 AND ranked.id <> ranked.canonical_id
    ON CONFLICT (intent_id, project_id, fact_id) DO NOTHING
    """,
    """
    WITH ranked AS (
        SELECT
            project_id,
            id,
            row_number() OVER (
                PARTITION BY project_id
                ORDER BY created_at NULLS LAST, id
            ) AS position
        FROM intents
        WHERE to_fact_id = 'goal'
    )
    DELETE FROM intents AS intent_row
    USING ranked
    WHERE ranked.position > 1
      AND intent_row.project_id = ranked.project_id
      AND intent_row.id = ranked.id
    """,
    """
    UPDATE intents
    SET lease_owner = NULL,
        lease_token = NULL,
        lease_expires_at = NULL
    WHERE to_fact_id = 'goal'
      AND (
          lease_owner IS NOT NULL
          OR lease_token IS NOT NULL
          OR lease_expires_at IS NOT NULL
      )
    """,
    """
    CREATE UNIQUE INDEX uq_intents_one_goal_per_project
    ON intents (project_id)
    WHERE to_fact_id = 'goal'
    """,
)


def upgrade() -> None:
    for statement in GOAL_INTENT_UNIQUENESS_UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Duplicate rows removed during upgrade remain available in audit_events,
    # but recreating them would violate later history and is intentionally not
    # attempted by downgrade.
    op.execute("DROP INDEX IF EXISTS uq_intents_one_goal_per_project")
