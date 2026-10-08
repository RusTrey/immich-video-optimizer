"""Job statuses and the groups built from them. The single source for Python and JS."""

QUEUED = "queued"
RUNNING = "running"
READY = "ready"
REJECTED_SAVING = "rejected_saving"
FAILED = "failed"
INTERRUPTED = "interrupted"
REPLACING = "replacing"
REPLACED = "replaced"
REPLACEMENT_INTERRUPTED = "replacement_interrupted"
RESTORING = "restoring"
RESTORE_INTERRUPTED = "restore_interrupted"
RESTORED = "restored"
DISCARDED = "discarded"
CANCELLED = "cancelled"

ALL = (
    QUEUED, RUNNING, READY, REJECTED_SAVING, FAILED, INTERRUPTED, REPLACING,
    REPLACED, REPLACEMENT_INTERRUPTED, RESTORING, RESTORE_INTERRUPTED,
    RESTORED, DISCARDED, CANCELLED,
)

# A video with a job in one of these statuses cannot get another job.
BLOCKING = (
    QUEUED, RUNNING, READY, REPLACING, REPLACED, REPLACEMENT_INTERRUPTED,
    RESTORING, RESTORE_INTERRUPTED,
)
RETRYABLE = (FAILED, INTERRUPTED, REJECTED_SAVING)
DISCARDABLE = (READY, FAILED, INTERRUPTED, REJECTED_SAVING)
# Interrupted file operations: files are inspected by SHA-1 and the job is
# completed or rolled back automatically when the state is unambiguous.
NEEDS_RECONCILE = (REPLACEMENT_INTERRUPTED, RESTORE_INTERRUPTED)
# Statuses that require an administrator decision or attention.
ATTENTION = (FAILED, INTERRUPTED, REPLACEMENT_INTERRUPTED, RESTORE_INTERRUPTED)

QUEUE_VIEW = (
    QUEUED, RUNNING, READY, REPLACING, FAILED, INTERRUPTED, REJECTED_SAVING,
    REPLACEMENT_INTERRUPTED,
)
JOURNAL_VIEW = (
    REPLACED, RESTORING, RESTORE_INTERRUPTED, RESTORED, REPLACEMENT_INTERRUPTED,
    FAILED, INTERRUPTED, REJECTED_SAVING, DISCARDED, CANCELLED,
)


def for_client():
    return {
        "all": list(ALL), "blocking": list(BLOCKING), "retryable": list(RETRYABLE),
        "discardable": list(DISCARDABLE), "needs_reconcile": list(NEEDS_RECONCILE),
        "attention": list(ATTENTION),
    }
