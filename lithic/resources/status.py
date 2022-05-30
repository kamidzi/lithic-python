# File generated from our OpenAPI spec by Stainless.

from typing import Dict, List, Union, Optional

from .._types import Timeout
from .._models import NoneModel, StringModel
from .._resource import SyncAPIResource, AsyncAPIResource
from .._base_client import AsyncPaginator, make_request_options
from ..types.api_status import *

__all__ = ["StatusResource", "AsyncStatusResource"]


class StatusResource(SyncAPIResource):
    def retrieve(
        self,
        *,
        headers: Optional[Dict[str, str]] = None,
        max_retries: Optional[int] = None,
        timeout: Optional[Union[float, Timeout]] = None,
    ) -> APIStatus:
        options = make_request_options(headers, max_retries, timeout)
        return self._get("/status", model=APIStatus, options=options)


class AsyncStatusResource(AsyncAPIResource):
    async def retrieve(
        self,
        *,
        headers: Optional[Dict[str, str]] = None,
        max_retries: Optional[int] = None,
        timeout: Optional[Union[float, Timeout]] = None,
    ) -> APIStatus:
        options = make_request_options(headers, max_retries, timeout)
        return await self._get("/status", model=APIStatus, options=options)
