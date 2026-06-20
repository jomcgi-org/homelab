# Tests for logger-name-missing-monolith-prefix rule.
import logging

# --- bad: string literal without monolith. prefix ---

# ruleid: logger-name-missing-monolith-prefix
logger = logging.getLogger("ships")

# ruleid: logger-name-missing-monolith-prefix
logger = logging.getLogger("hikes")

# ruleid: logger-name-missing-monolith-prefix
logger = logging.getLogger("dr_jobs")

# ruleid: logger-name-missing-monolith-prefix
logger = logging.getLogger("trips.backfill")

# --- ok: correct monolith. prefix ---

# ok: starts with monolith.
logger = logging.getLogger("monolith.ships.router")

# ok: starts with monolith.
logger = logging.getLogger("monolith.hikes.jobs")

# ok: starts with monolith.
logger = logging.getLogger("monolith.dr_jobs.scraper")

# ok: __name__ resolves at runtime and is outside the string-literal pattern
logger = logging.getLogger(__name__)

# ok: excluded third-party library logger names
logging.getLogger("discord.gateway")
logging.getLogger("discord.client")
logging.getLogger("httpx")
logging.getLogger("httpcore")
logging.getLogger("uvicorn")
logging.getLogger("uvicorn.access")
logging.getLogger("uvicorn.error")
