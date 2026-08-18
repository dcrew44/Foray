from collections.abc import Iterable


class AdapterError(Exception):
    """Base class for all adapter failures.

    Callers that want blanket handling (e.g. the per-posting loop in a
    board fetch) can catch this instead of enumerating subclasses.
    """


class EmptyDataError(AdapterError):
    def __init__(self, message: str = "adapter received empty data"):
        super().__init__(message)


class FieldNotFoundError(AdapterError):
    """A field the adapter requires is absent from the source payload."""

    def __init__(self, expected: str, keys: Iterable[str]):
        self.expected = expected
        self.keys = sorted(keys)
        super().__init__(
            f"expected field {expected!r} not found; "
            f"payload has: {', '.join(self.keys) or '<no keys>'}"
        )
