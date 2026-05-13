from services.celery_tasks import app
# Import side-effect: registers vector-index tasks with the Celery app.
import services.celery_tasks_vector  # noqa: F401

worker_options = [
    "worker",
    "--loglevel=INFO",
    "--concurrency=4",
    "--hostname=miniblog_worker@%h",
    "--pool=threads",
]

if __name__ == "__main__":
    print("Starting MiniBlog podcast agent workers...")
    app.worker_main(worker_options)