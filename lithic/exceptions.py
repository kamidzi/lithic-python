import json

from httpx import Request, Response
from typing_extensions import Literal


class APIError(Exception):
    message: str
    request: Request

    def __init__(self, message: str, request: Request) -> None:
        super().__init__(message)
        self.request = request
        self.message = message


class APIResponseValidationError(APIError):
    response: Response
    status_code: int

    def __init__(self, request: Request, response: Response) -> None:
        super().__init__("Data returned by API invalid for expected schema.", request)
        self.response = response
        self.status_code = response.status_code


class APIStatusError(APIError):
    """Raised when an API response has a status code of 4xx or 5xx."""

    response: Response
    status_code: int

    def __init__(self, message: str, request: Request, response: Response) -> None:
        super().__init__(message, request)
        self.response = response
        self.status_code = response.status_code


def make_status_error(request: Request, response: Response) -> APIStatusError:
    err_text = response.text.strip()
    try:
        err_msg = json.loads(err_text)
    except:
        err_msg = err_text or "Unknown"

    if response.status_code == 400:
        return BadRequestError(err_msg, request, response)
    if response.status_code == 401:
        return AuthenticationError(err_msg, request, response)
    if response.status_code == 403:
        return PermissionDeniedError(err_msg, request, response)
    if response.status_code == 404:
        return NotFoundError(err_msg, request, response)
    if response.status_code == 409:
        return ConflictError(err_msg, request, response)
    if response.status_code == 422:
        return UnprocessableEntityError(err_msg, request, response)
    if response.status_code == 429:
        return RateLimitError(err_msg, request, response)
    if response.status_code >= 500:
        return InternalServerError(err_msg, request, response)
    return APIStatusError(err_msg, request, response)


class BadRequestError(APIStatusError):
    status_code: Literal[400]

    def __init__(self, message: str, request: Request, response: Response) -> None:
        super().__init__(message, request, response)
        self.status_code = 400


class AuthenticationError(APIStatusError):
    status_code: Literal[401]

    def __init__(self, message: str, request: Request, response: Response) -> None:
        super().__init__(message, request, response)
        self.status_code = 401


class PermissionDeniedError(APIStatusError):
    status_code: Literal[403]

    def __init__(self, message: str, request: Request, response: Response) -> None:
        super().__init__(message, request, response)
        self.status_code = 403


class NotFoundError(APIStatusError):
    status_code: Literal[404]

    def __init__(self, message: str, request: Request, response: Response) -> None:
        super().__init__(message, request, response)
        self.status_code = 404


class ConflictError(APIStatusError):
    status_code: Literal[409]

    def __init__(self, message: str, request: Request, response: Response) -> None:
        super().__init__(message, request, response)
        self.status_code = 409


class UnprocessableEntityError(APIStatusError):
    status_code: Literal[422]

    def __init__(self, message: str, request: Request, response: Response) -> None:
        super().__init__(message, request, response)
        self.status_code = 422


class RateLimitError(APIStatusError):
    status_code: Literal[429]

    def __init__(self, message: str, request: Request, response: Response) -> None:
        super().__init__(message, request, response)
        self.status_code = 429


class InternalServerError(APIStatusError):
    status_code: int

    def __init__(self, message: str, request: Request, response: Response) -> None:
        super().__init__(message, request, response)
        self.status_code = response.status_code


class APIConnectionError(APIError):
    def __init__(self, request: Request, message: str = "Connection error.") -> None:
        super().__init__(message, request)


class APITimeoutError(APIConnectionError):
    def __init__(self, request: Request) -> None:
        super().__init__(request, "Request timed out.")
