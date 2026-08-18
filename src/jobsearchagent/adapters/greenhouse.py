import html
import logging
from datetime import datetime
from typing import Any

import httpx2

from jobsearchagent.adapters.errors import (
    EmptyDataError,
    FieldNotFoundError,
)
from jobsearchagent.jobposting import JobPosting

GREENHOUSE_API_URL = "https://boards-api.greenhouse.io/v1/boards/"
USER_AGENT = "jobsearchagent/1.0"

logger = logging.getLogger(__name__)


def parse_compensation(data):
    return None


def format_post_date(data: dict[str, Any]) -> datetime | None:
    try:
        if "first_published" in data:
            return datetime.fromisoformat(data["first_published"])
    except ValueError:
        return None
    return None


def format_description(data: dict[str, Any]) -> str:
    if "content" not in data:
        raise FieldNotFoundError("content", data.keys())
    content = html.unescape(data["content"])
    return content


def format_location(data: dict[str, Any]) -> list[str]:
    if "location" not in data:
        raise FieldNotFoundError("location", data.keys())
    location_str = (data.get("location") or {}).get("name")
    location = location_str.split("; ") if location_str or []
    return location


def build_greenhouse_posting(data: dict[str, Any], company_slug: str) -> JobPosting:
    if not data:
        raise EmptyDataError()
    source = "greenhouse"
    company = str(data["company_name"])
    post_id = str(data["id"])
    uid = source + ":" + company_slug + ":" + post_id

    title = data["title"]
    posted_date = format_post_date(data)
    description = format_description(data)
    salary = parse_compensation(description)
    location = format_location(data)
    url = data["absolute_url"]

    return JobPosting(
        id=uid,
        title=title,
        company=company,
        location=location,
        salary=salary,
        posted_date=posted_date,
        description=description,
        source=source,
        url=url,
    )


async def fetch_board(url: str) -> dict[str, Any] | None:
    # TODO: Make a universal, likely put in a shared base.py
    headers = {"User-Agent": USER_AGENT}
    async with httpx2.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=30.0)
            response.raise_for_status()
            return response.json()
        except httpx2.HTTPError as e:
            logger.error(e)
            return None


async def fetch_greenhouse_board(company_slug) -> list[JobPosting]:
    url = GREENHOUSE_API_URL + company_slug + "/jobs?content=true"
    data = await fetch_board(url)

    if not data:
        logger.warning(f"Empty fetch_board response at {url}")
        return []

    posting_list = []
    for raw_post in data["jobs"]:
        try:
            posting = build_greenhouse_posting(raw_post, company_slug)
            posting_list.append(posting)
        except EmptyDataError:
            logger.warning("Empty data passed to build_greenhouse_posting")
        except FieldNotFoundError as e:
            logger.error(e)
    return posting_list
