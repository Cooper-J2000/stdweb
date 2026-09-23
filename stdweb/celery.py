import os
import logging

from celery import Celery
from celery.signals import worker_ready

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'stdweb.settings')

app = Celery('stdweb')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Additional configuration for better task management
app.conf.update(
    # Re-queue tasks if worker is lost (crash, kill, etc.)
    task_reject_on_worker_lost=True,
    # Store task state for chain tracking
    task_track_started=True,
    #
    worker_prefetch_multiplier=1,
    broker_transport_options={
        "visibility_timeout": 3600,
    },
    # Use SIGTERM for graceful shutdown (allows cleanup handlers to run)
    worker_term_signal='SIGTERM',

)

# Load task modules from all registered Django apps.
app.autodiscover_tasks(related_name='celery_tasks')


logger = logging.getLogger(__name__)


@worker_ready.connect
def _clear_stale_task_pids(sender=None, **kwargs):
    """Drop `celery_pid` values left over from a previous worker / previous boot.

    Such a PID is stale by definition (the process that stored it is gone) and
    PIDs get reused across reboots, so a stale value must never reach
    kill_task_processes() / os.killpg().  Live pids are (re)written by
    TaskProcessContext.__enter__() when a task actually starts.
    """
    try:
        from . import models
        cleared = models.Task.objects.filter(celery_pid__isnull=False).update(celery_pid=None)
        if cleared:
            logger.info('Cleared %d stale task celery_pid value(s) at worker startup', cleared)
    except Exception:
        logger.exception('Could not clear stale celery_pid values at worker startup')


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
