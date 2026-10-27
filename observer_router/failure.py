"""The failure one tier attempt returns."""


class TierFailure(Exception):
    """Raised when a tier cannot produce usable content.

    retry_at/quota are set when the provider said which quota ran out and when it resets.
    partial is (served model, text) of a reply cut off at the token limit: served when no later
    tier answers, since claude-mem keeps a cut-off reply's complete blocks. overloaded is why
    the model, not this key, looks out of capacity: "unavailable" (HTTP 503), "exhausted" (an
    HTTP 429 that names no quota), or "timeout" (a socket read timeout that used the provider's
    whole budget); every key in the provider's group would fail the same way. skipped is set
    when the provider was never called -- its lane stayed busy, or too little of the request
    deadline was left -- which says nothing about its health.
    """

    def __init__(self, message: str, retry_at: float | None = None, quota: str = "",
                 overloaded: str = "", partial: tuple[str, str] | None = None,
                 skipped: bool = False):
        super().__init__(message)
        self.retry_at = retry_at
        self.quota = quota
        self.partial = partial
        self.overloaded = overloaded
        self.skipped = skipped
