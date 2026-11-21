"""Typed errors. The CLI maps SynergyError subclasses to exit code 3."""


class SynergyError(Exception):
    pass


class ValidationError(SynergyError):
    pass


class NotFound(SynergyError):
    pass


class DuplicateOperator(SynergyError):
    pass


class TaskExists(SynergyError):
    pass


class WorkerExists(SynergyError):
    pass


class NotActive(SynergyError):
    pass


class NotOwner(SynergyError):
    pass


class BadAck(SynergyError):
    pass


class MissingEvidence(SynergyError):
    pass


class NotClaimable(SynergyError):
    pass


class DependencyNotDone(SynergyError):
    pass


class WorkerLimit(SynergyError):
    pass


class AlreadyOwned(SynergyError):
    def __init__(self, task_id: str, owner: str, since: str) -> None:
        super().__init__(f"task {task_id} is already owned by {owner} since {since}; not claimed")
        self.owner = owner


class FileOwned(SynergyError):
    def __init__(self, path: str, owner: str, held: str) -> None:
        super().__init__(f"{path} is covered by an exclusive claim on {held} held by {owner}; not claimed")
        self.owner = owner


class AuthFailed(SynergyError):
    pass
