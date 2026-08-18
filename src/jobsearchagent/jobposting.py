from datetime import datetime
from typing import NamedTuple

from pydantic import BaseModel


class SalaryRange(NamedTuple):
    min: int
    max: int


class JobPosting(BaseModel):
    id: str
    title: str
    company: str
    location: list[str]
    salary: SalaryRange | None = None
    posted_date: datetime | None = None
    description: str
    source: str
    url: str

    def __str__(self):
        return f'JobPosting(id={self.id}, title="{self.title}", posted_date={self.posted_date}, url={self.url})'
