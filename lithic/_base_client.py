from __future__ import annotations

import json
import time
import inspect
import platform
from random import random
from typing import (
    Any,
    Dict,
    Type,
    Union,
    Generic,
    Mapping,
    TypeVar,
    Iterable,
    Iterator,
    Optional,
    Generator,
    AsyncIterator,
    cast,
)

import anyio
import httpx
import pydantic
from pydantic import PrivateAttr

from ._types import (
    Query,
    ModelT,
    Timeout,
    NotGiven,
    Transport,
    ProxiesTypes,
    RequestOptions,
)
from ._models import BaseModel, NoneModel, GenericModel, FinalRequestOptions
from .exceptions import (
    APITimeoutError,
    APIConnectionError,
    APIResponseValidationError,
    make_status_error,
)

# TODO: make base page type vars covariant
SyncPageT = TypeVar("SyncPageT", bound="BaseSyncPage[Any]")
AsyncPageT = TypeVar("AsyncPageT", bound="BaseAsyncPage[Any]")


PageParamsT = TypeVar("PageParamsT", bound=Mapping[str, object])
ResponseT = TypeVar("ResponseT", bound=BaseModel)

DEFAULT_TIMEOUT = Timeout(timeout=60.0, connect=5.0)
DEFAULT_MAX_RETRIES = 2

# https://github.com/encode/httpx/discussions/2181
BUILD_REQUEST_PARAMS = inspect.signature(
    httpx.Client.build_request  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
).parameters.values()


class BasePage(GenericModel, Generic[ModelT, PageParamsT]):
    _options: FinalRequestOptions = PrivateAttr()
    _model: Type[ModelT] = PrivateAttr()

    def has_next_page(self) -> bool:
        return self.next_page_params() is not None

    def next_page_params(self) -> Optional[PageParamsT]:
        ...

    def _get_page_items(self) -> Iterable[ModelT]:
        ...


class BaseSyncPage(BasePage[ModelT, Mapping[str, object]], Generic[ModelT]):
    _client: SyncAPIClient = pydantic.PrivateAttr()

    def _set_private_attributes(
        self,
        client: SyncAPIClient,
        model: Type[ModelT],
        options: FinalRequestOptions,
    ) -> None:
        self._model = model
        self._client = client
        self._options = options

    # Pydantic uses a custom `__iter__` method to support casting BaseModels
    # to dictionaries. e.g. dict(model).
    # As we want to support `for item in page`, this is inherently incompatible
    # with the default pydantic behaviour. It is not possible to support both
    # use cases at once. Fortunately, this is not a big deal as all other pydantic
    # methods should continue to work as expected as there is an alternative method
    # to cast a model to a dictionary, model.dict(), which is used internally
    # by pydantic.
    def __iter__(self) -> Iterator[ModelT]:  # type: ignore
        for page in self.iter_pages():
            for item in page._get_page_items():
                yield item

    def iter_pages(self: SyncPageT) -> Iterator[SyncPageT]:
        page = self
        while True:
            yield page
            if page.has_next_page():
                page = page.get_next_page()
            else:
                return

    def get_next_page(self: SyncPageT) -> SyncPageT:
        next_params = self.next_page_params()
        if not next_params:
            raise RuntimeError(
                "No next page expected; please check `.has_next_page()` before calling `.get_next_page()`."
            )

        options = self._options.copy()
        options.params = {**options.params, **next_params}

        return self._client.request_api_list(self._model, page=self.__class__, options=options)


class AsyncPaginator(Generic[ModelT, AsyncPageT]):
    def __init__(
        self,
        client: AsyncAPIClient,
        options: FinalRequestOptions,
        page_cls: Type[AsyncPageT],
        model: Type[ModelT],
    ) -> None:
        self._model = model
        self._client = client
        self._options = options
        self._page_cls = page_cls

    def __await__(self) -> Generator[Any, None, AsyncPageT]:
        return self._get_page().__await__()

    async def _get_page(self) -> AsyncPageT:
        page = await self._client.request(self._page_cls, self._options)
        page._set_private_attributes(  # pyright: ignore[reportPrivateUsage]
            model=self._model,
            options=self._options,
            client=self._client,
        )
        return page

    async def __aiter__(self) -> AsyncIterator[ModelT]:
        # https://github.com/microsoft/pyright/issues/3464
        page = cast(
            AsyncPageT,
            await self,  # type: ignore
        )
        async for item in page:
            yield item


class BaseAsyncPage(BasePage[ModelT, Mapping[str, object]], Generic[ModelT]):
    _client: AsyncAPIClient = pydantic.PrivateAttr()

    def _set_private_attributes(
        self,
        model: Type[ModelT],
        client: AsyncAPIClient,
        options: FinalRequestOptions,
    ) -> None:
        self._model = model
        self._client = client
        self._options = options

    async def __aiter__(self) -> AsyncIterator[ModelT]:
        async for page in self.iter_pages():
            for item in page._get_page_items():
                yield item

    async def iter_pages(self: AsyncPageT) -> AsyncIterator[AsyncPageT]:
        page = self
        while True:
            yield page
            if page.has_next_page():
                page = await page.get_next_page()
            else:
                return

    async def get_next_page(self: AsyncPageT) -> AsyncPageT:
        next_params = self.next_page_params()
        if not next_params:
            raise RuntimeError(
                "No next page expected; please check `.has_next_page()` before calling `.get_next_page()`."
            )

        options = self._options.copy()
        options.params = {**options.params, **next_params}
        return await self._client.request_api_list(self._model, page=self.__class__, options=options)


class BaseClient:
    _version: str
    api_key: str
    max_retries: int
    timeout: Union[float, Timeout, None]
    _strict_response_validation: bool

    def __init__(
        self,
        version: str,
        api_key: str,
        _strict_response_validation: bool,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: Union[float, Timeout, None] = DEFAULT_TIMEOUT,
    ) -> None:
        self._version = version
        self.api_key = api_key
        self.max_retries = max_retries
        self.timeout = timeout
        self._strict_response_validation = _strict_response_validation

    def remaining_retries(
        self,
        remaining_retries: Optional[int],
        options: FinalRequestOptions,
    ) -> int:
        return remaining_retries if remaining_retries is not None else options.get_max_retries(self.max_retries)

    def prepare_request_args(
        self,
        options: FinalRequestOptions,
    ) -> Dict[str, Any]:
        return options.to_request_args(default_headers=self.default_headers(), default_timeout=self.timeout)

    def process_response(
        self, model: Type[ResponseT], options: FinalRequestOptions, response: httpx.Response
    ) -> ResponseT:
        if model is NoneModel:
            return model()
        data = (
            response.json()
            if response.headers.get("content-type") == "application/json"
            else {"content": response.text}
        )
        if self._strict_response_validation:
            return model(**data)
        else:
            return model.construct(**data)

    def default_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent(),
            "X-Stainless-Client-User-Agent": self.platform_properties(),
        }

    def user_agent(self) -> str:
        return f"{self.__class__.__name__}/Python {self._version}"

    def platform_properties(self) -> str:
        arch, _ = platform.architecture()
        properties = {
            "lang": "python",
            "packageVersion": self._version,
            "os": platform.system(),
            "arch": arch,
            "runtime": platform.python_implementation(),
            "runtimeVersion": platform.python_version(),
        }
        return json.dumps(properties)

    def calculate_retry_timeout(
        self,
        remaining_retries: int,
        options: FinalRequestOptions,
        response_headers: Optional[httpx.Headers] = None,
    ) -> float:
        max_retries = options.get_max_retries(self.max_retries)
        try:
            # About the Retry-After header: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Retry-After
            #
            # TODO: we may want to handle the case where the header is using the http-date syntax: "Retry-After:
            # <http-date>". See https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Retry-After#syntax for
            # details.
            retry_after = -1 if response_headers is None else int(response_headers.get("retry-after"))
        except:
            retry_after = -1

        # If the API asks us to wait a certain amount of time (and it's a reasonable amount), just do what it says.
        if 0 < retry_after <= 60:
            return retry_after

        initial_retry_delay = 0.5
        max_retry_delay = 2.0
        nb_retries = max_retries - remaining_retries

        # Apply exponential backoff, but not more than the max.
        sleep_seconds = min(initial_retry_delay * pow(nb_retries - 1, 2), max_retry_delay)

        # Apply some jitter, plus-or-minus half a second.
        jitter = random() - 0.5
        timeout = sleep_seconds + jitter
        return timeout if timeout >= 0 else 0

    def should_retry(self, response: httpx.Response) -> bool:
        # Note: this is not a standard header
        should_retry_header = response.headers.get("x-should-retry")

        # If the server explicitly says whether or not to retry, obey.
        if should_retry_header == "true":
            return True
        if should_retry_header == "false":
            return False

        # Retry on lock timeouts.
        if response.status_code == 409:
            return True

        # Retry on rate limits.
        if response.status_code == 429:
            return True

        # Retry internal errors.
        if response.status_code >= 500:
            return True

        return False


class SyncAPIClient(BaseClient):
    _client: httpx.Client

    def __init__(
        self,
        *,
        version: str,
        base_url: str,
        api_key: str,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: Union[float, Timeout, None] = DEFAULT_TIMEOUT,
        transport: Optional[Transport] = None,
        proxies: Optional[ProxiesTypes] = None,
        _strict_response_validation: bool,
    ) -> None:
        super().__init__(version, api_key, _strict_response_validation, max_retries, timeout)
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            proxies=proxies,  # type: ignore
            transport=transport,  # type: ignore
            headers={"Accept": "application/json"},
        )

    def request(
        self,
        model: Type[ResponseT],
        options: FinalRequestOptions,
        remaining_retries: Optional[int] = None,
    ) -> ResponseT:
        retries = self.remaining_retries(remaining_retries, options)
        req_args = self.prepare_request_args(options)

        # https://github.com/encode/httpx/discussions/2181
        request = self._client.build_request(**req_args)  # pyright: ignore[reportUnknownMemberType]

        try:
            response = self._client.send(request)
            response.raise_for_status()
        except httpx.HTTPStatusError as err:  # thrown on 4xx and 5xx status code
            if retries > 0 and self.should_retry(err.response):
                return self.retry_request(options, model, retries, err.response.headers)
            raise make_status_error(request, err.response)
        except httpx.TimeoutException as err:
            if retries > 0:
                return self.retry_request(options, model, retries)
            raise APITimeoutError(request=request) from err
        except Exception as err:
            if retries > 0:
                return self.retry_request(options, model, retries)
            raise APIConnectionError(request=request) from err

        try:
            rsp = self.process_response(model, options, response)
        except pydantic.ValidationError as err:
            raise APIResponseValidationError(request=request, response=response) from err

        return rsp

    def retry_request(
        self,
        options: FinalRequestOptions,
        model: Type[ResponseT],
        remaining_retries: int,
        response_headers: Optional[httpx.Headers] = None,
    ) -> ResponseT:
        remaining = remaining_retries - 1
        timeout = self.calculate_retry_timeout(remaining, options, response_headers)
        print(f"Retry request in {timeout} seconds")
        # In a synchronous context we are blocking the entire thread. Up to the library user to run the client in a
        # different thread if necessary.
        time.sleep(timeout)
        return self.request(
            options=options,
            model=model,
            remaining_retries=remaining,
        )

    def request_api_list(
        self,
        model: Type[ModelT],
        page: Type[SyncPageT],
        options: FinalRequestOptions,
    ) -> SyncPageT:
        resp = self.request(page, options)
        resp._set_private_attributes(  # pyright: ignore[reportPrivateUsage]
            client=self,
            model=model,
            options=options,
        )
        return resp

    def get(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        query: Query = {},
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="get", url=path, params=query, **options)
        return self.request(model, opts)

    def post(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        body: Query | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="post", url=path, json_data=body, **options)
        return self.request(model, opts)

    def patch(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        body: Query | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="patch", url=path, json_data=body, **options)
        return self.request(model, opts)

    def put(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        body: Query | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="put", url=path, json_data=body, **options)
        return self.request(model, opts)

    def delete(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        body: Query | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="delete", url=path, json_data=body, **options)
        return self.request(model, opts)

    def get_api_list(
        self,
        path: str,
        *,
        model: Type[ModelT],
        page: Type[SyncPageT],
        query: Query = {},
        options: RequestOptions = {},
    ) -> SyncPageT:
        opts = FinalRequestOptions(method="get", url=path, params=query, **options)
        return self.request_api_list(model, page, opts)


class AsyncAPIClient(BaseClient):
    _client: httpx.AsyncClient

    def __init__(
        self,
        *,
        version: str,
        base_url: str,
        api_key: str,
        _strict_response_validation: bool,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: Union[float, Timeout, None] = DEFAULT_TIMEOUT,
        transport: Optional[Transport] = None,
        proxies: Optional[ProxiesTypes] = None,
    ) -> None:
        super().__init__(version, api_key, _strict_response_validation, max_retries, timeout)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            proxies=proxies,  # type: ignore
            transport=transport,  # type: ignore
            headers={"Accept": "application/json"},
        )

    async def request(
        self,
        model: Type[ResponseT],
        options: FinalRequestOptions,
        remaining_retries: Optional[int] = None,
    ) -> ResponseT:
        retries = self.remaining_retries(remaining_retries, options)
        req_args = self.prepare_request_args(options)

        # https://github.com/encode/httpx/discussions/2181
        request = self._client.build_request(**req_args)  # pyright: ignore[reportUnknownMemberType]

        try:
            response = await self._client.send(request)
            response.raise_for_status()
        except httpx.HTTPStatusError as err:  # thrown on 4xx and 5xx status code
            if retries > 0 and self.should_retry(err.response):
                return await self.retry_request(options, model, retries, err.response.headers)
            raise make_status_error(request, err.response)
        except httpx.ConnectTimeout as err:
            if retries > 0:
                return await self.retry_request(options, model, retries)
            raise APITimeoutError(request=request) from err
        except httpx.ReadTimeout as err:
            # We explicitly do not retry on ReadTimeout errors as this means
            # that the server processing the request has taken 60 seconds
            # (our default timeout). This likely indicates that something
            # is not working as expected on the server side.
            raise
        except httpx.TimeoutException as err:
            if retries > 0:
                return await self.retry_request(options, model, retries)
            raise APITimeoutError(request=request) from err
        except Exception as err:
            if retries > 0:
                return await self.retry_request(options, model, retries)
            raise APIConnectionError(request=request) from err

        try:
            rsp = self.process_response(model, options, response)
        except pydantic.ValidationError as err:
            raise APIResponseValidationError(request=request, response=response) from err

        return rsp

    async def retry_request(
        self,
        options: FinalRequestOptions,
        model: Type[ResponseT],
        remaining_retries: int,
        response_headers: Optional[httpx.Headers] = None,
    ) -> ResponseT:
        remaining = remaining_retries - 1
        timeout = self.calculate_retry_timeout(remaining, options, response_headers)
        print(f"Retry request in {timeout} seconds")
        await anyio.sleep(timeout)
        return await self.request(
            options=options,
            model=model,
            remaining_retries=remaining,
        )

    def request_api_list(
        self,
        model: Type[ModelT],
        page: Type[AsyncPageT],
        options: FinalRequestOptions,
    ) -> AsyncPaginator[ModelT, AsyncPageT]:
        return AsyncPaginator(client=self, options=options, page_cls=page, model=model)

    async def get(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        query: Query = {},
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="get", url=path, params=query, **options)
        return await self.request(model, opts)

    async def post(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        body: Query | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="post", url=path, json_data=body, **options)
        return await self.request(model, opts)

    async def patch(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        body: Query | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="patch", url=path, json_data=body, **options)
        return await self.request(model, opts)

    async def put(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        body: Query | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="put", url=path, json_data=body, **options)
        return await self.request(model, opts)

    async def delete(
        self,
        path: str,
        *,
        model: Type[ResponseT],
        body: Query | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions(method="delete", url=path, json_data=body, **options)
        return await self.request(model, opts)

    def get_api_list(
        self,
        path: str,
        *,
        model: Type[ModelT],
        page: Type[AsyncPageT],
        query: Query = {},
        options: RequestOptions = {},
    ) -> AsyncPaginator[ModelT, AsyncPageT]:
        opts = FinalRequestOptions(method="get", url=path, params=query, **options)
        return self.request_api_list(model, page, opts)


def make_request_options(
    headers: Dict[str, str] | NotGiven,
    max_retries: int | NotGiven,
    timeout: float | Timeout | None | NotGiven,
) -> RequestOptions:
    """Create a dict of type RequestOptions without keys of NotGiven values."""
    options: RequestOptions = {}
    if not isinstance(headers, NotGiven):
        options["headers"] = headers

    if not isinstance(max_retries, NotGiven):
        options["max_retries"] = max_retries

    if not isinstance(timeout, NotGiven):
        options["timeout"] = timeout

    return options
