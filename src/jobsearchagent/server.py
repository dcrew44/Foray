import asyncio
import logging

from jobsearchagent.adapters.greenhouse import fetch_greenhouse_board

logger = logging.getLogger(__name__)


if __name__ == "__main__":
    postings = asyncio.run(fetch_greenhouse_board("anthropic"))[:5]
    for posting in postings:
        print(posting)
    postings = asyncio.run(fetch_greenhouse_board("doordashusa"))[:5]
    for posting in postings:
        print(posting)
    postings = asyncio.run(fetch_greenhouse_board("airbnb"))[:5]
    for posting in postings:
        print(posting)
