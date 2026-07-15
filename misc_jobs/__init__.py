"""Task modules for the miscellaneous EP sync jobs runner (run_misc_jobs.py).

Each module here exposes a ``run() -> None`` callable that performs one small,
periodic export. Register it in ``run_misc_jobs.py``'s ``JOBS`` list and give it
a schedule in ``misc_jobs_schedule.yaml``; the nightly Civis job runs it on the
weekday(s) that schedule names.
"""
