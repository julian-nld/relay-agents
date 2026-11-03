"""Settings → System (relay.system): the machine, Relay's boxes and what they take, storage; and
stopping an idle box, removing a stopped WorkBox or an image nothing uses."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from starlette.requests import Request

from relay import system
from relay.web import ApiError

if TYPE_CHECKING:
    from relay.app import Relay


async def get_system(request: Request, relay: Relay):
    return await asyncio.to_thread(system.overview, relay)


async def get_storage(request: Request, relay: Relay):
    return await asyncio.to_thread(system.storage, relay)


async def stop_box(request: Request, relay: Relay):
    try:
        await asyncio.to_thread(system.stop, relay, request.path_params["name"])
    except system.BoxError as exc:
        raise ApiError(str(exc), 409) from exc
    return await get_system(request, relay)


async def remove_box(request: Request, relay: Relay):
    try:
        await asyncio.to_thread(system.remove, relay, request.path_params["name"])
    except system.BoxError as exc:
        raise ApiError(str(exc), 409) from exc
    return await get_system(request, relay)


async def remove_stopped(request: Request, relay: Relay):
    gone = await asyncio.to_thread(system.remove_stopped, relay)
    return {**await get_system(request, relay), "removed": gone}


async def remove_image(request: Request, relay: Relay):
    try:
        await asyncio.to_thread(system.remove_image, relay, request.path_params["image_id"])
    except system.BoxError as exc:
        raise ApiError(str(exc), 409) from exc
    return await get_storage(request, relay)
