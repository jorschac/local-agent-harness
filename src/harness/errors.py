"""Public errors deliberately exclude exception reprs and provider credentials."""
from __future__ import annotations


class HarnessError(Exception):
    def __init__(self, message: str, code: str = "invalid_request", status: int = 400, param: str | None = None, error_type: str = "invalid_request_error"):
        super().__init__(message)
        self.message, self.code, self.status, self.param, self.error_type = message, code, status, param, error_type

    def body(self) -> dict:
        value = {"message": self.message, "type": self.error_type, "code": self.code}
        if self.param is not None:
            value["param"] = self.param
        return {"error": value}


def safe_error(exc: BaseException) -> dict:
    if isinstance(exc, HarnessError):
        return exc.body()
    return HarnessError("Operation failed; inspect local configuration or plugin implementation", "internal_error", 500, error_type="server_error").body()
