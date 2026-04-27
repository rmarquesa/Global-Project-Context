from __future__ import annotations

from dataclasses import dataclass, asdict

import psycopg

from gpc.config import POSTGRES_DSN


@dataclass(frozen=True)
class RetentionResult:
    mcp_days: int
    token_days: int
    dry_run: bool
    mcp_calls: int
    token_savings_samples: int

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def apply_retention(
    *,
    mcp_days: int = 30,
    token_days: int = 90,
    dry_run: bool = False,
) -> RetentionResult:
    mcp_days = max(1, int(mcp_days))
    token_days = max(1, int(token_days))

    with psycopg.connect(POSTGRES_DSN) as conn:
        if dry_run:
            token_count = conn.execute(
                """
                select count(*) from gpc_token_savings_samples
                where created_at < now() - (%s::text || ' days')::interval
                """,
                (str(token_days),),
            ).fetchone()[0]
            mcp_count = conn.execute(
                """
                select count(*) from gpc_mcp_calls
                where called_at < now() - (%s::text || ' days')::interval
                """,
                (str(mcp_days),),
            ).fetchone()[0]
            return RetentionResult(
                mcp_days=mcp_days,
                token_days=token_days,
                dry_run=True,
                mcp_calls=int(mcp_count or 0),
                token_savings_samples=int(token_count or 0),
            )

        token_deleted = conn.execute(
            """
            delete from gpc_token_savings_samples
            where created_at < now() - (%s::text || ' days')::interval
            """,
            (str(token_days),),
        ).rowcount or 0
        mcp_deleted = conn.execute(
            """
            delete from gpc_mcp_calls
            where called_at < now() - (%s::text || ' days')::interval
            """,
            (str(mcp_days),),
        ).rowcount or 0

    return RetentionResult(
        mcp_days=mcp_days,
        token_days=token_days,
        dry_run=False,
        mcp_calls=int(mcp_deleted),
        token_savings_samples=int(token_deleted),
    )
