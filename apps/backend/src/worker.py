import logging

from arq.connections import RedisSettings

from src.core.config import settings
from src.pipeline.run_campaign import run_campaign as _run_campaign

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker")


async def startup(ctx):
    logger.info("worker: ready")


async def shutdown(ctx):
    logger.info("worker: shutting down")


async def run_campaign(ctx, campaign_id: int):
    """arq task entrypoint. Delegates to the orchestrator.
    The orchestrator is itself idempotent, so re-enqueuing on the same campaign_id
    after a crash will resume cleanly."""
    logger.info("running campaign %s", campaign_id)
    await _run_campaign(campaign_id)
    logger.info("campaign %s done", campaign_id)


class WorkerSettings:
    functions = [run_campaign]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    # 30 minutes — comfortably above the architecture's 5-8 min realistic per-campaign time.
    job_timeout = 1800
    # If a worker crashes mid-job, arq lets the next worker pick it up. Each stage
    # is idempotent, so retries are safe.
    max_tries = 3
