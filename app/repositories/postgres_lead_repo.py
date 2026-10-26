import ipaddress
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.core.database import get_pool
from app.models.lead import LeadResponse
from app.repositories.protocol import LeadRepositoryProtocol

_SEARCH_FIELDS = ["name", "email", "phone", "message", "company"]
_SORT_WHITELIST = {
    "created_at",
    "updated_at",
    "spam_score",
    "status",
    "honeypot_triggered",
    "ip_address",
}

_SELECT_COLUMNS = """
    id, widget_id, tenant_id, form_data, ip_address, user_agent, referer,
    fingerprint, geo_country, geo_city, geo_region, geo_isp, geo_provider,
    spam_score, spam_reasons, honeypot_triggered, status, created_at, updated_at
"""


def _inet(ip: str) -> str:
    """Return a value safe for the INET column.

    The in-memory repo tolerates non-IP values like "unknown" or
    "localhost", which are invalid INET literals in Postgres. Use a
    sentinel so the constraint can never reject a submission.
    """
    try:
        ipaddress.ip_address(ip)
        return ip
    except ValueError:
        return "0.0.0.0"


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def _sort_expr(sort_by: str, sort_order: str) -> str:
    column = sort_by if sort_by in _SORT_WHITELIST else "created_at"
    direction = "ASC" if sort_order.lower() == "asc" else "DESC"
    return f"{column} {direction}"


class PostgresLeadRepository(LeadRepositoryProtocol):
    @staticmethod
    def _row_to_response(row) -> LeadResponse:
        return LeadResponse(
            id=row["id"],
            widget_id=row["widget_id"],
            tenant_id=row["tenant_id"],
            form_data=_parse_json(row["form_data"]),
            ip_address=str(row["ip_address"]),
            user_agent=row.get("user_agent"),
            referer=row.get("referer"),
            fingerprint=row["fingerprint"],
            geo_country=row.get("geo_country"),
            geo_city=row.get("geo_city"),
            geo_region=row.get("geo_region"),
            geo_isp=row.get("geo_isp"),
            geo_provider=row.get("geo_provider"),
            spam_score=row.get("spam_score", 0.0),
            spam_reasons=_parse_json(row.get("spam_reasons")),
            honeypot_triggered=row.get("honeypot_triggered", False),
            status=row.get("status", "pending"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def create(
        self,
        widget_id: str,
        tenant_id: str,
        form_data: dict[str, Any],
        ip_address: str,
        fingerprint: str,
        user_agent: str | None = None,
        referer: str | None = None,
        spam_score: float = 0.0,
        spam_reasons: list[str] | None = None,
        honeypot_triggered: bool = False,
        status: str = "pending",
    ) -> LeadResponse:
        pool = await get_pool()
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO leads (
                    widget_id, tenant_id, form_data, ip_address, user_agent,
                    referer, fingerprint, spam_score, spam_reasons,
                    honeypot_triggered, status, created_at, updated_at
                )
                VALUES ($1, $2, $3::jsonb, $4::inet, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13)
                RETURNING {_SELECT_COLUMNS}
                """,
                widget_id,
                tenant_id,
                json.dumps(form_data),
                _inet(ip_address),
                user_agent,
                referer,
                fingerprint,
                spam_score,
                json.dumps(spam_reasons) if spam_reasons is not None else None,
                honeypot_triggered,
                status,
                now,
                now,
            )
        return self._row_to_response(row)

    async def get_by_id(self, lead_id: str) -> LeadResponse | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_SELECT_COLUMNS} FROM leads WHERE id = $1",
                lead_id,
            )
        if row is None:
            return None
        return self._row_to_response(row)

    async def list_by_widget(
        self,
        widget_id: str,
        tenant_id: str,
        include_honeypot: bool = False,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
        status: str | None = None,
        spam_min: float | None = None,
        spam_max: float | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[LeadResponse], int]:
        conditions = ["widget_id = $1", "tenant_id = $2"]
        params: list[Any] = [widget_id, tenant_id]
        return await self._list(
            conditions,
            params,
            include_honeypot,
            page,
            page_size,
            search,
            status,
            spam_min,
            spam_max,
            date_from,
            date_to,
            sort_by,
            sort_order,
        )

    async def list_by_tenant(
        self,
        tenant_id: str,
        include_honeypot: bool = False,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
        status: str | None = None,
        spam_min: float | None = None,
        spam_max: float | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[LeadResponse], int]:
        conditions = ["tenant_id = $1"]
        params: list[Any] = [tenant_id]
        return await self._list(
            conditions,
            params,
            include_honeypot,
            page,
            page_size,
            search,
            status,
            spam_min,
            spam_max,
            date_from,
            date_to,
            sort_by,
            sort_order,
        )

    async def _list(
        self,
        conditions: list[str],
        params: list[Any],
        include_honeypot: bool,
        page: int,
        page_size: int,
        search: str | None,
        status: str | None,
        spam_min: float | None,
        spam_max: float | None,
        date_from: date | None,
        date_to: date | None,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[LeadResponse], int]:
        if not include_honeypot:
            conditions.append("honeypot_triggered = FALSE")

        if search:
            terms = " OR ".join(
                f"form_data ->> '{field}' ILIKE ${len(params) + 1}"
                for field in _SEARCH_FIELDS
            )
            conditions.append(f"({terms})")
            params.append(f"%{search}%")

        if status:
            conditions.append(f"status = ${len(params) + 1}")
            params.append(status)
        if spam_min is not None:
            conditions.append(f"spam_score >= ${len(params) + 1}")
            params.append(spam_min)
        if spam_max is not None:
            conditions.append(f"spam_score <= ${len(params) + 1}")
            params.append(spam_max)
        if date_from:
            conditions.append(f"created_at >= ${len(params) + 1}")
            params.append(date_from)
        if date_to:
            conditions.append(f"created_at <= ${len(params) + 1}")
            params.append(date_to)

        where = " AND ".join(conditions)
        sort = _sort_expr(sort_by, sort_order)
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2

        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM leads WHERE {where}", *params
            )
            rows = await conn.fetch(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM leads WHERE {where}
                ORDER BY {sort}
                LIMIT ${limit_idx} OFFSET ${offset_idx}
                """,
                *params,
                page_size,
                (page - 1) * page_size,
            )
        items = [self._row_to_response(r) for r in rows]
        return items, total

    async def get_stats(self, widget_id: str, tenant_id: str) -> dict[str, Any]:
        return await self._stats(
            "widget_id = $1 AND tenant_id = $2", [widget_id, tenant_id]
        )

    async def get_tenant_stats(self, tenant_id: str) -> dict[str, Any]:
        return await self._stats("tenant_id = $1", [tenant_id])

    async def _stats(self, where: str, params: list[Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)
        thirty_days_ago = now - timedelta(days=30)

        non_honeypot = f"NOT honeypot_triggered AND {where}"
        pool = await get_pool()
        async with pool.acquire() as conn:
            total_leads = await conn.fetchval(
                f"SELECT COUNT(*) FROM leads WHERE {non_honeypot}", *params
            )
            today = await conn.fetchval(
                f"SELECT COUNT(*) FROM leads WHERE {non_honeypot} AND created_at >= ${len(params) + 1}",
                *params,
                today_start,
            )
            this_week = await conn.fetchval(
                f"SELECT COUNT(*) FROM leads WHERE {non_honeypot} AND created_at >= ${len(params) + 1}",
                *params,
                week_start,
            )
            this_month = await conn.fetchval(
                f"SELECT COUNT(*) FROM leads WHERE {non_honeypot} AND created_at >= ${len(params) + 1}",
                *params,
                month_start,
            )
            avg_spam_score = await conn.fetchval(
                f"SELECT AVG(spam_score) FROM leads WHERE {non_honeypot}", *params
            )
            honeypot_blocked = await conn.fetchval(
                f"SELECT COUNT(*) FROM leads WHERE honeypot_triggered AND {where}",
                *params,
            )
            country_rows = await conn.fetch(
                f"""
                SELECT geo_country AS country, COUNT(*) AS count
                FROM leads WHERE {where} AND geo_country IS NOT NULL
                GROUP BY geo_country ORDER BY count DESC LIMIT 10
                """,
                *params,
            )
            time_rows = await conn.fetch(
                f"""
                SELECT (created_at AT TIME ZONE 'UTC')::date AS date, COUNT(*) AS count
                FROM leads
                WHERE {non_honeypot} AND created_at >= ${len(params) + 1}
                GROUP BY date ORDER BY date
                """,
                *params,
                thirty_days_ago,
            )

        return {
            "total_leads": total_leads,
            "today": today,
            "this_week": this_week,
            "this_month": this_month,
            "avg_spam_score": round(float(avg_spam_score or 0.0), 4),
            "honeypot_blocked": honeypot_blocked,
            "top_countries": [
                {"country": r["country"], "count": r["count"]} for r in country_rows
            ],
            "leads_over_time": [
                {"date": r["date"].isoformat(), "count": r["count"]} for r in time_rows
            ],
        }

    async def get_export_data(
        self,
        widget_id: str,
        tenant_id: str,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["widget_id = $1", "tenant_id = $2"]
        params: list[Any] = [widget_id, tenant_id]
        if date_from:
            conditions.append(f"created_at >= ${len(params) + 1}")
            params.append(date_from)
        if date_to:
            conditions.append(f"created_at <= ${len(params) + 1}")
            params.append(date_to)

        where = " AND ".join(conditions)
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM leads WHERE {where}
                ORDER BY created_at DESC
                """,
                *params,
            )
        return [self._row_to_response(r).model_dump() for r in rows]

    async def update_status(
        self, lead_id: str, status: str, **extra
    ) -> LeadResponse | None:
        geo_columns = {
            "geo_country": "geo_country",
            "geo_city": "geo_city",
            "geo_region": "geo_region",
            "geo_isp": "geo_isp",
            "geo_provider": "geo_provider",
        }
        assignments = []
        geo_params: list[Any] = []
        for key, column in geo_columns.items():
            if key in extra:
                assignments.append(
                    f"{column} = COALESCE(${len(geo_params) + 4}, {column})"
                )
                geo_params.append(extra[key])

        set_clause = "status = $2, updated_at = $3"
        if assignments:
            set_clause += ", " + ", ".join(assignments)

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE leads SET {set_clause}
                WHERE id = $1
                RETURNING {_SELECT_COLUMNS}
                """,
                lead_id,
                status,
                datetime.now(timezone.utc),
                *geo_params,
            )
        if row is None:
            return None
        return self._row_to_response(row)

    async def delete(self, lead_id: str, widget_id: str, tenant_id: str) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                DELETE FROM leads WHERE id = $1 AND widget_id = $2 AND tenant_id = $3
                RETURNING id
                """,
                lead_id,
                widget_id,
                tenant_id,
            )
        return row is not None

    async def batch_delete(
        self, lead_ids: list[str], widget_id: str, tenant_id: str
    ) -> int:
        if not lead_ids:
            return 0
        pool = await get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                """
                WITH deleted AS (
                    DELETE FROM leads
                    WHERE id = ANY($1::uuid[]) AND widget_id = $2 AND tenant_id = $3
                    RETURNING 1
                )
                SELECT COUNT(*) FROM deleted
                """,
                lead_ids,
                widget_id,
                tenant_id,
            )
        return count
