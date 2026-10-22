"""A finite candidate-to-handoff window, not a test-success detector."""


class CloseoutMonitor:
    def __init__(self, started, policy=None):
        self.started = started
        self.policy = dict(policy) if policy is not None else None
        self.first_candidate = None
        self.warned = False

    def observe(self, changed_paths, now):
        # Actual snapshot differences count, not write success or self-report.
        # Reverting or repeatedly editing does not restart the window.
        if self.policy is not None and changed_paths and self.first_candidate is None:
            self.first_candidate = now

    def check(self, now, can_warn=True):
        if self.first_candidate is None:
            return None
        age = now - self.first_candidate
        if age >= self.policy['deadline_seconds']:
            return 'deadline'
        if can_warn and not self.warned and age >= self.policy['warn_seconds']:
            self.warned = True
            return 'warn'
        return None

    def snapshot(self, now):
        return {
            'enabled': self.policy is not None,
            'first_candidate_at': None if self.first_candidate is None else self.first_candidate - self.started,
            'candidate_age_seconds': None if self.first_candidate is None else now - self.first_candidate,
            'warned': self.warned,
            'policy': self.policy,
            'scope': 'attempt_handoff_budget_not_validation_evidence',
        }
