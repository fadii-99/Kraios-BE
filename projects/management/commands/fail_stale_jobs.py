"""Close out jobs whose worker died, so clients stop polling them forever.

Nothing but the worker running a job ever moves it out of QUEUED/PROCESSING,
so stopping the stack mid-generation leaves the row stuck in PROCESSING and
the frontend polling `/projects/jobs/{id}/` with no worker left to answer it.

Run this after the workers are down (or with `--older-than` set above the
longest real generation), never against jobs a live worker still holds.
"""
from django.core.management.base import BaseCommand

from projects.services import fail_stale_jobs


class Command(BaseCommand):
    help = 'Mark abandoned QUEUED/PROCESSING jobs as FAILED.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--older-than',
            type=int,
            default=0,
            metavar='MINUTES',
            help=(
                'Only close jobs created at least this many minutes ago. '
                'Default 0 (every unfinished job) — safe only while the '
                'workers are stopped.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List what would be closed without writing anything.',
        )

    def handle(self, *args, **options):
        older_than = options['older_than']
        dry_run = options['dry_run']

        closed = fail_stale_jobs(older_than_minutes=older_than, dry_run=dry_run)

        if not closed:
            self.stdout.write('No unfinished jobs to close.')
            return

        for job in closed:
            self.stdout.write(
                f'{"would close" if dry_run else "closed"} '
                f'{job.id}  {job.job_type}  project={job.project_id}'
            )

        summary = f'{len(closed)} job(s) {"would be closed" if dry_run else "closed"}.'
        self.stdout.write(self.style.SUCCESS(summary))
