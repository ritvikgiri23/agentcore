from celery import Celery

from app.core.config import get_settings
from app.core.logging import configure_logging

configure_logging()

settings = get_settings()

celery_app = Celery(
    "agentcore",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_pool="prefork",
    # Ack only once a run is done, and requeue if the worker process dies mid-run: the
    # runner's lease and atomic claim make a redelivery harmless.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Runs are long; don't let one worker hoard queued runs it hasn't started.
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
)
