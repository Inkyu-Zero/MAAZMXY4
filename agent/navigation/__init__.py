"""Resource-driven, bounded native navigation."""


class NavigationError(ValueError):
    def __init__(self, message, *, reason, candidates=()):
        super().__init__(message)
        self.reason = reason
        self.candidates = tuple(candidates)
