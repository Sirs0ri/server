"""
Controller to stream audio to players.

The streams controller hosts a basic, unprotected HTTP-only webserver
purely to stream audio packets to players and some control endpoints such as
the upnp callbacks and json rpc api for slimproto clients.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

import shortuuid
from aiohttp import web

from music_assistant.common.helpers.util import get_ip, select_free_port
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import ConfigEntryType, ContentType
from music_assistant.common.models.errors import MediaNotFoundError, QueueEmpty
from music_assistant.common.models.media_items import AudioFormat
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import (
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_CROSSFADE_DURATION,
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_OUTPUT_CHANNELS,
    CONF_OUTPUT_CODEC,
    CONF_PUBLISH_IP,
)
from music_assistant.server.helpers.audio import (
    check_audio_support,
    crossfade_pcm_parts,
    get_media_stream,
    get_stream_details,
)
from music_assistant.server.helpers.process import AsyncProcess
from music_assistant.server.helpers.util import get_ips
from music_assistant.server.helpers.webserver import Webserver
from music_assistant.server.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import CoreConfig
    from music_assistant.common.models.player import Player


DEFAULT_STREAM_HEADERS = {
    "transferMode.dlna.org": "Streaming",
    "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000",  # noqa: E501
    "Cache-Control": "no-cache",
    "Connection": "close",
    "icy-name": "Music Assistant",
    "icy-pub": "0",
}
FLOW_MAX_SAMPLE_RATE = 96000
FLOW_MAX_BIT_DEPTH = 24


class MultiClientStreamJob:
    """Representation of a (multiclient) Audio Queue stream job/task.

    The whole idea here is that in case of a player (sync)group,
    all client players receive the exact same (PCM) audio chunks from the source audio.
    A StreamJob is tied to a Queue and streams the queue flow stream,
    In case a stream is restarted (e.g. when seeking), a new MultiClientStreamJob will be created.
    """

    def __init__(
        self,
        stream_controller: StreamsController,
        queue_id: str,
        pcm_format: AudioFormat,
        start_queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Initialize MultiClientStreamJob instance."""
        self.stream_controller = stream_controller
        self.queue_id = queue_id
        self.queue = self.stream_controller.mass.player_queues.get(queue_id)
        assert self.queue  # just in case
        self.pcm_format = pcm_format
        self.start_queue_item = start_queue_item
        self.seek_position = seek_position
        self.fade_in = fade_in
        self.job_id = shortuuid.uuid()
        self.expected_players: set[str] = set()
        self.subscribed_players: dict[str, asyncio.Queue[bytes]] = {}
        self.bytes_streamed: int = 0
        self.client_seconds_skipped: dict[str, int] = {}
        self._all_clients_connected = asyncio.Event()
        # start running the audio task in the background
        self._audio_task = asyncio.create_task(self._stream_job_runner())
        self.logger = stream_controller.logger.getChild(f"streamjob_{self.job_id}")
        self._finished: bool = False

    @property
    def finished(self) -> bool:
        """Return if this StreamJob is finished."""
        return self._finished or self._audio_task.done()

    @property
    def pending(self) -> bool:
        """Return if this Job is pending start."""
        return not self.finished and not self._all_clients_connected.is_set()

    @property
    def running(self) -> bool:
        """Return if this Job is running."""
        return not self.finished and not self.pending

    def stop(self) -> None:
        """Stop running this job."""
        self._finished = True
        if self._audio_task.done():
            return
        self._audio_task.cancel()
        for sub_queue in self.subscribed_players.values():
            with suppress(asyncio.QueueFull):
                sub_queue.put_nowait(b"")

    async def resolve_stream_url(
        self,
        child_player_id: str,
    ) -> str:
        """Resolve the childplayer specific stream URL to this streamjob."""
        output_codec = ContentType(
            await self.stream_controller.mass.config.get_player_config_value(
                child_player_id, CONF_OUTPUT_CODEC
            )
        )
        fmt = output_codec.value
        # handle raw pcm
        if output_codec.is_pcm():
            player = self.stream_controller.mass.players.get(child_player_id)
            player_max_bit_depth = 32 if player.supports_24bit else 16
            output_sample_rate = min(self.pcm_format.sample_rate, player.max_sample_rate)
            output_bit_depth = min(self.pcm_format.bit_depth, player_max_bit_depth)
            output_channels = await self.stream_controller.mass.config.get_player_config_value(
                child_player_id, CONF_OUTPUT_CHANNELS
            )
            channels = 1 if output_channels != "stereo" else 2
            fmt += (
                f";codec=pcm;rate={output_sample_rate};"
                f"bitrate={output_bit_depth};channels={channels}"
            )
        url = f"{self.stream_controller._server.base_url}/{self.queue_id}/multi/{self.job_id}/{child_player_id}/{self.start_queue_item.queue_item_id}.{fmt}"  # noqa: E501
        self.expected_players.add(child_player_id)
        return url

    async def subscribe(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Subscribe consumer and iterate incoming chunks on the queue."""
        try:
            self.subscribed_players[player_id] = sub_queue = asyncio.Queue(2)

            if self._all_clients_connected.is_set():
                # client subscribes while we're already started
                self.logger.debug(
                    "Client %s is joining while the stream is already started", player_id
                )
                # calculate how many seconds the client missed so far
                self.client_seconds_skipped[player_id] = (
                    self.bytes_streamed / self.pcm_format.pcm_sample_size
                )
            else:
                self.logger.debug("Subscribed client %s", player_id)

            if len(self.subscribed_players) == len(self.expected_players):
                # we reached the number of expected subscribers, set event
                # so that chunks can be pushed
                self._all_clients_connected.set()

            # keep reading audio chunks from the queue until we receive an empty one
            while True:
                chunk = await sub_queue.get()
                if chunk == b"":
                    # EOF chunk received
                    break
                yield chunk
        finally:
            self.subscribed_players.pop(player_id, None)
            self.logger.debug("Unsubscribed client %s", player_id)
            # check if this was the last subscriber and we should cancel
            await asyncio.sleep(2)
            if len(self.subscribed_players) == 0 and self._audio_task and not self.finished:
                self.logger.debug("Cleaning up, all clients disappeared...")
                self._audio_task.cancel()

    async def _put_chunk(self, chunk: bytes) -> None:
        """Put chunk of data to all subscribers."""
        async with asyncio.TaskGroup() as tg:
            for sub_queue in list(self.subscribed_players.values()):
                # put this chunk on the player's subqueue
                tg.create_task(sub_queue.put(chunk))
        self.bytes_streamed += len(chunk)

    async def _stream_job_runner(self) -> None:
        """Feed audio chunks to StreamJob subscribers."""
        chunk_num = 0
        async for chunk in self.stream_controller.get_flow_stream(
            self.queue, self.start_queue_item, self.pcm_format, self.seek_position, self.fade_in
        ):
            if chunk_num == 0:
                # wait until all expected clients are connected
                try:
                    async with asyncio.timeout(10):
                        await self._all_clients_connected.wait()
                except TimeoutError:
                    if len(self.subscribed_players) == 0:
                        self.stream_controller.logger.error(
                            "Abort multi client stream job for queue %s: "
                            "clients did not connect within timeout",
                            self.queue.display_name,
                        )
                        break
                    # not all clients connected but timeout expired, set flag and move on
                    # with all clients that did connect
                    self._all_clients_connected.set()
                else:
                    self.stream_controller.logger.debug(
                        "Starting multi client stream job for queue %s "
                        "with %s out of %s connected clients",
                        self.queue.display_name,
                        len(self.subscribed_players),
                        len(self.expected_players),
                    )
            await self._put_chunk(chunk)
            chunk_num += 1

        # mark EOF with empty chunk
        await self._put_chunk(b"")


def parse_pcm_info(content_type: str) -> tuple[int, int, int]:
    """Parse PCM info from a codec/content_type string."""
    params = (
        dict(urllib.parse.parse_qsl(content_type.replace(";", "&"))) if ";" in content_type else {}
    )
    sample_rate = int(params.get("rate", 44100))
    sample_size = int(params.get("bitrate", 16))
    channels = int(params.get("channels", 2))
    return (sample_rate, sample_size, channels)


class StreamsController(CoreController):
    """Webserver Controller to stream audio to players."""

    domain: str = "streams"

    def __init__(self, *args, **kwargs):
        """Initialize instance."""
        super().__init__(*args, **kwargs)
        self._server = Webserver(self.logger, enable_dynamic_routes=True)
        self.multi_client_jobs: dict[str, MultiClientStreamJob] = {}
        self.register_dynamic_route = self._server.register_dynamic_route
        self.unregister_dynamic_route = self._server.unregister_dynamic_route
        self.manifest.name = "Streamserver"
        self.manifest.description = (
            "Music Assistant's core server that is responsible for "
            "streaming audio to players on the local network as well as "
            "some player specific local control callbacks."
        )
        self.manifest.icon = "cast-audio"

    @property
    def base_url(self) -> str:
        """Return the base_url for the streamserver."""
        return self._server.base_url

    async def get_config_entries(
        self,
        action: str | None = None,  # noqa: ARG002
        values: dict[str, ConfigValueType] | None = None,  # noqa: ARG002
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        default_ip = await get_ip()
        all_ips = await get_ips()
        default_port = await select_free_port(8096, 9200)
        return (
            ConfigEntry(
                key=CONF_BIND_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=default_port,
                label="TCP Port",
                description="The TCP port to run the server. "
                "Make sure that this server can be reached "
                "on the given IP and TCP port by players on the local network.",
            ),
            ConfigEntry(
                key=CONF_PUBLISH_IP,
                type=ConfigEntryType.STRING,
                default_value=default_ip,
                label="Published IP address",
                description="This IP address is communicated to players where to find this server. "
                "Override the default in advanced scenarios, such as multi NIC configurations. \n"
                "Make sure that this server can be reached "
                "on the given IP and TCP port by players on the local network. \n"
                "This is an advanced setting that should normally "
                "not be adjusted in regular setups.",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                default_value="0.0.0.0",
                options=(ConfigValueOption(x, x) for x in {"0.0.0.0", *all_ips}),
                label="Bind to IP/interface",
                description="Start the stream server on this specific interface. \n"
                "Use 0.0.0.0 to bind to all interfaces, which is the default. \n"
                "This is an advanced setting that should normally "
                "not be adjusted in regular setups.",
                advanced=True,
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        ffmpeg_present, libsoxr_support, version = await check_audio_support()
        if not ffmpeg_present:
            self.logger.error("FFmpeg binary not found on your system, playback will NOT work!.")
        elif not libsoxr_support:
            self.logger.warning(
                "FFmpeg version found without libsoxr support, "
                "highest quality audio not available. "
            )
        self.logger.info(
            "Detected ffmpeg version %s %s",
            version,
            "with libsoxr support" if libsoxr_support else "",
        )
        # start the webserver
        self.publish_port = config.get_value(CONF_BIND_PORT)
        self.publish_ip = config.get_value(CONF_PUBLISH_IP)
        await self._server.setup(
            bind_ip=config.get_value(CONF_BIND_IP),
            bind_port=self.publish_port,
            base_url=f"http://{self.publish_ip}:{self.publish_port}",
            static_routes=[
                (
                    "*",
                    "/{queue_id}/multi/{job_id}/{player_id}/{queue_item_id}.{fmt}",
                    self.serve_multi_subscriber_stream,
                ),
                (
                    "*",
                    "/{queue_id}/flow/{queue_item_id}.{fmt}",
                    self.serve_queue_flow_stream,
                ),
                (
                    "*",
                    "/{queue_id}/single/{queue_item_id}.{fmt}",
                    self.serve_queue_item_stream,
                ),
            ],
        )

    async def close(self) -> None:
        """Cleanup on exit."""
        await self._server.close()

    async def resolve_stream_url(
        self,
        queue_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        flow_mode: bool = False,
    ) -> str:
        """Resolve the (regular, single player) stream URL for the given QueueItem.

        This is called just-in-time by the Queue controller to get the URL to the audio.
        """
        output_codec = ContentType(
            await self.mass.config.get_player_config_value(queue_id, CONF_OUTPUT_CODEC)
        )
        fmt = output_codec.value
        # handle raw pcm
        if output_codec.is_pcm():
            player = self.mass.players.get(queue_id)
            player_max_bit_depth = 32 if player.supports_24bit else 16
            if flow_mode:
                output_sample_rate = min(FLOW_MAX_SAMPLE_RATE, player.max_sample_rate)
                output_bit_depth = min(FLOW_MAX_BIT_DEPTH, player_max_bit_depth)
            else:
                streamdetails = await get_stream_details(self.mass, queue_item)
                output_sample_rate = min(
                    streamdetails.audio_format.sample_rate, player.max_sample_rate
                )
                output_bit_depth = min(streamdetails.audio_format.bit_depth, player_max_bit_depth)
            output_channels = await self.mass.config.get_player_config_value(
                queue_id, CONF_OUTPUT_CHANNELS
            )
            channels = 1 if output_channels != "stereo" else 2
            fmt += (
                f";codec=pcm;rate={output_sample_rate};"
                f"bitrate={output_bit_depth};channels={channels}"
            )
        query_params = {}
        base_path = "flow" if flow_mode else "single"
        url = f"{self._server.base_url}/{queue_id}/{base_path}/{queue_item.queue_item_id}.{fmt}"
        if seek_position:
            query_params["seek_position"] = str(seek_position)
        if fade_in:
            query_params["fade_in"] = "1"
        if query_params:
            url += "?" + urllib.parse.urlencode(query_params)
        return url

    async def create_multi_client_stream_job(
        self,
        queue_id: str,
        start_queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> MultiClientStreamJob:
        """Create a MultiClientStreamJob for the given queue..

        This is called by player/sync group implementations to start streaming
        the queue audio to multiple players at once.
        """
        if existing_job := self.multi_client_jobs.pop(queue_id, None):  # noqa: SIM102
            # cleanup existing job first
            if not existing_job.finished:
                existing_job.stop()

        self.multi_client_jobs[queue_id] = stream_job = MultiClientStreamJob(
            self,
            queue_id=queue_id,
            pcm_format=AudioFormat(
                # hardcoded pcm quality of 48/24 for now
                # TODO: change this to the highest quality supported by all child players ?
                content_type=ContentType.from_bit_depth(24),
                sample_rate=48000,
                bit_depth=24,
                channels=2,
            ),
            start_queue_item=start_queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
        )
        return stream_job

    async def serve_queue_item_stream(self, request: web.Request) -> web.Response:
        """Stream single queueitem audio to a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        queue_player = self.mass.players.get(queue_id)
        queue_item_id = request.match_info["queue_item_id"]
        queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
        if not queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {queue_item_id}")
        try:
            streamdetails = await get_stream_details(self.mass, queue_item=queue_item)
        except MediaNotFoundError:
            raise web.HTTPNotFound(
                reason=f"Unable to retrieve streamdetails for item: {queue_item}"
            )
        seek_position = int(request.query.get("seek_position", 0))
        fade_in = bool(request.query.get("fade_in", 0))
        # work out output format/details
        output_format = await self._get_output_format(
            output_format_str=request.match_info["fmt"],
            queue_player=queue_player,
            default_sample_rate=streamdetails.audio_format.sample_rate,
            default_bit_depth=streamdetails.audio_format.bit_depth,
        )

        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": f"audio/{output_format.output_format_str}",
        }
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        await resp.prepare(request)

        # return early if this is only a HEAD request
        if request.method == "HEAD":
            return resp

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving audio stream for QueueItem %s to %s", queue_item.uri, queue.display_name
        )

        # collect player specific ffmpeg args to re-encode the source PCM stream
        pcm_format = AudioFormat(
            content_type=ContentType.from_bit_depth(streamdetails.audio_format.bit_depth),
            sample_rate=streamdetails.audio_format.sample_rate,
            bit_depth=streamdetails.audio_format.bit_depth,
        )
        ffmpeg_args = await self._get_player_ffmpeg_args(
            queue_player,
            input_format=pcm_format,
            output_format=output_format,
        )

        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:
            # feed stdin with pcm audio chunks from origin
            async def read_audio():
                try:
                    async for chunk in get_media_stream(
                        self.mass,
                        streamdetails=streamdetails,
                        pcm_format=pcm_format,
                        seek_position=seek_position,
                        fade_in=fade_in,
                    ):
                        try:
                            await ffmpeg_proc.write(chunk)
                        except BrokenPipeError:
                            break
                finally:
                    ffmpeg_proc.write_eof()

            ffmpeg_proc.attach_task(read_audio())

            # read final chunks from stdout
            async for chunk in ffmpeg_proc.iter_any(768000):
                try:
                    await resp.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # race condition
                    break

        return resp

    async def serve_queue_flow_stream(self, request: web.Request) -> web.Response:
        """Stream Queue Flow audio to player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        start_queue_item_id = request.match_info["queue_item_id"]
        start_queue_item = self.mass.player_queues.get_item(queue_id, start_queue_item_id)
        if not start_queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {start_queue_item_id}")
        seek_position = int(request.query.get("seek_position", 0))
        fade_in = bool(request.query.get("fade_in", 0))
        queue_player = self.mass.players.get(queue_id)
        # work out output format/details
        output_format = await self._get_output_format(
            output_format_str=request.match_info["fmt"],
            queue_player=queue_player,
            default_sample_rate=FLOW_MAX_SAMPLE_RATE,
            default_bit_depth=FLOW_MAX_BIT_DEPTH,
        )
        # prepare request, add some DLNA/UPNP compatible headers
        enable_icy = request.headers.get("Icy-MetaData", "") == "1"
        icy_meta_interval = 65536 if output_format.content_type.is_lossless() else 8192
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": f"audio/{output_format.output_format_str}",
        }
        if enable_icy:
            headers["icy-metaint"] = str(icy_meta_interval)

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        await resp.prepare(request)

        # return early if this is only a HEAD request
        if request.method == "HEAD":
            return resp

        # all checks passed, start streaming!
        self.logger.debug("Start serving Queue flow audio stream for %s", queue_player.name)

        # collect player specific ffmpeg args to re-encode the source PCM stream
        pcm_format = AudioFormat(
            content_type=ContentType.from_bit_depth(output_format.bit_depth),
            sample_rate=output_format.sample_rate,
            bit_depth=output_format.bit_depth,
            channels=2,
        )
        ffmpeg_args = await self._get_player_ffmpeg_args(
            queue_player,
            input_format=pcm_format,
            output_format=output_format,
        )

        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:
            # feed stdin with pcm audio chunks from origin
            async def read_audio():
                try:
                    async for chunk in self.get_flow_stream(
                        queue=queue,
                        start_queue_item=start_queue_item,
                        pcm_format=pcm_format,
                        seek_position=seek_position,
                        fade_in=fade_in,
                    ):
                        try:
                            await ffmpeg_proc.write(chunk)
                        except BrokenPipeError:
                            break
                finally:
                    ffmpeg_proc.write_eof()

            ffmpeg_proc.attach_task(read_audio())

            # read final chunks from stdout
            iterator = (
                ffmpeg_proc.iter_chunked(icy_meta_interval)
                if enable_icy
                else ffmpeg_proc.iter_any(768000)
            )
            async for chunk in iterator:
                try:
                    await resp.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # race condition
                    break

                if not enable_icy:
                    continue

                # if icy metadata is enabled, send the icy metadata after the chunk
                current_item = self.mass.player_queues.get_item(
                    queue.queue_id, queue.index_in_buffer
                )
                if (
                    current_item
                    and current_item.streamdetails
                    and current_item.streamdetails.stream_title
                ):
                    title = current_item.streamdetails.stream_title
                elif queue and current_item and current_item.name:
                    title = current_item.name
                else:
                    title = "Music Assistant"
                metadata = f"StreamTitle='{title}';".encode()
                while len(metadata) % 16 != 0:
                    metadata += b"\x00"
                length = len(metadata)
                length_b = chr(int(length / 16)).encode()
                await resp.write(length_b + metadata)

        return resp

    async def serve_multi_subscriber_stream(self, request: web.Request) -> web.Response:
        """Stream Queue Flow audio to a child player within a multi subscriber setup."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        streamjob = self.multi_client_jobs.get(queue_id)
        if not streamjob:
            raise web.HTTPNotFound(reason=f"Unknown StreamJob for queue: {queue_id}")
        job_id = request.match_info["job_id"]
        if job_id != streamjob.job_id:
            raise web.HTTPNotFound(reason=f"StreamJob ID {job_id} mismatch for queue: {queue_id}")
        child_player_id = request.match_info["player_id"]
        child_player = self.mass.players.get(child_player_id)
        if not child_player:
            raise web.HTTPNotFound(reason=f"Unknown player: {child_player_id}")
        # work out (childplayer specific!) output format/details
        output_format = await self._get_output_format(
            output_format_str=request.match_info["fmt"],
            queue_player=child_player,
            default_sample_rate=streamjob.pcm_format.sample_rate,
            default_bit_depth=streamjob.pcm_format.bit_depth,
        )
        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": f"audio/{output_format.output_format_str}",
        }
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        await resp.prepare(request)

        # return early if this is only a HEAD request
        if request.method == "HEAD":
            return resp

        # some players (e.g. dlna, sonos) misbehave and do multiple GET requests
        # to the stream in an attempt to get the audio details such as duration
        # which is a bit pointless for our duration-less queue stream
        # and it completely messes with the subscription logic
        if child_player_id in streamjob.subscribed_players:
            self.logger.warning(
                "Player %s is making multiple requests "
                "to the same stream, playback may be disturbed!",
                child_player_id,
            )

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving multi-subscriber Queue flow audio stream for queue %s to player %s",
            streamjob.queue.display_name,
            child_player.display_name,
        )

        # collect player specific ffmpeg args to re-encode the source PCM stream
        ffmpeg_args = await self._get_player_ffmpeg_args(
            child_player,
            input_format=streamjob.pcm_format,
            output_format=output_format,
        )

        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:
            # feed stdin with pcm audio chunks from origin
            async def read_audio():
                try:
                    async for chunk in streamjob.subscribe(child_player_id):
                        try:
                            await ffmpeg_proc.write(chunk)
                        except BrokenPipeError:
                            break
                finally:
                    ffmpeg_proc.write_eof()

            ffmpeg_proc.attach_task(read_audio())

            # read final chunks from stdout
            async for chunk in ffmpeg_proc.iter_any(768000):
                try:
                    await resp.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # race condition
                    break

        return resp

    async def get_flow_stream(
        self,
        queue: PlayerQueue,
        start_queue_item: QueueItem,
        pcm_format: AudioFormat,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> AsyncGenerator[bytes, None]:
        """Get a flow stream of all tracks in the queue."""
        # ruff: noqa: PLR0915
        assert pcm_format.content_type.is_pcm()
        queue_track = None
        last_fadeout_part = b""
        total_bytes_written = 0
        self.logger.info("Start Queue Flow stream for Queue %s", queue.display_name)

        while True:
            # get (next) queue item to stream
            if queue_track is None:
                queue_track = start_queue_item
                use_crossfade = queue.crossfade_enabled
            else:
                seek_position = 0
                fade_in = False
                try:
                    (
                        _,
                        queue_track,
                        use_crossfade,
                    ) = await self.mass.player_queues.preload_next_url(queue.queue_id)
                except QueueEmpty:
                    break

            # get streamdetails
            try:
                streamdetails = await get_stream_details(self.mass, queue_track)
            except MediaNotFoundError as err:
                # streamdetails retrieval failed, skip to next track instead of bailing out...
                self.logger.warning(
                    "Skip track %s due to missing streamdetails",
                    queue_track.name,
                    exc_info=err,
                )
                continue

            self.logger.debug(
                "Start Streaming queue track: %s (%s) for queue %s - crossfade: %s",
                streamdetails.uri,
                queue_track.name,
                queue.display_name,
                use_crossfade,
            )

            # set some basic vars
            pcm_sample_size = int(pcm_format.sample_rate * (pcm_format.bit_depth / 8) * 2)
            crossfade_duration = self.mass.config.get_raw_player_config_value(
                queue.queue_id, CONF_CROSSFADE_DURATION, 8
            )
            crossfade_size = int(pcm_sample_size * crossfade_duration)
            queue_track.streamdetails.seconds_skipped = seek_position
            buffer_size = crossfade_size if use_crossfade else int(pcm_sample_size * 2)

            buffer = b""
            bytes_written = 0
            chunk_num = 0
            # handle incoming audio chunks
            async for chunk in get_media_stream(
                self.mass,
                streamdetails,
                pcm_format=pcm_format,
                seek_position=seek_position,
                fade_in=fade_in,
                # only allow strip silence from begin if track is being crossfaded
                strip_silence_begin=last_fadeout_part != b"",
            ):
                chunk_num += 1

                ####  HANDLE FIRST PART OF TRACK

                # buffer full for crossfade
                if last_fadeout_part and (len(buffer) >= buffer_size):
                    first_part = buffer + chunk
                    # perform crossfade
                    fadein_part = first_part[:crossfade_size]
                    remaining_bytes = first_part[crossfade_size:]
                    crossfade_part = await crossfade_pcm_parts(
                        fadein_part,
                        last_fadeout_part,
                        pcm_format.bit_depth,
                        pcm_format.sample_rate,
                    )
                    # send crossfade_part
                    yield crossfade_part
                    bytes_written += len(crossfade_part)
                    # also write the leftover bytes from the strip action
                    if remaining_bytes:
                        yield remaining_bytes
                        bytes_written += len(remaining_bytes)

                    # clear vars
                    last_fadeout_part = b""
                    buffer = b""
                    continue

                # enough data in buffer, feed to output
                if len(buffer) >= (buffer_size * 2):
                    yield buffer[:buffer_size]
                    bytes_written += buffer_size
                    buffer = buffer[buffer_size:] + chunk
                    continue

                # all other: fill buffer
                buffer += chunk
                continue

            #### HANDLE END OF TRACK

            if bytes_written == 0:
                # stream error: got empty first chunk ?!
                self.logger.warning("Stream error on %s", streamdetails.uri)
                queue_track.streamdetails.seconds_streamed = 0
                continue

            if buffer and use_crossfade:
                # if crossfade is enabled, save fadeout part to pickup for next track
                last_fadeout_part = buffer[-crossfade_size:]
                remaining_bytes = buffer[:-crossfade_size]
                yield remaining_bytes
                bytes_written += len(remaining_bytes)
            elif buffer:
                # no crossfade enabled, just yield the buffer last part
                yield buffer
                bytes_written += len(buffer)

            # end of the track reached - store accurate duration
            queue_track.streamdetails.seconds_streamed = bytes_written / pcm_sample_size
            total_bytes_written += bytes_written
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
            )

        self.logger.info("Finished Queue Flow stream for Queue %s", queue.display_name)

    async def _get_player_ffmpeg_args(
        self,
        player: Player,
        input_format: AudioFormat,
        output_format: AudioFormat,
    ) -> list[str]:
        """Get player specific arguments for the given (pcm) input and output details."""
        player_conf = await self.mass.config.get_player_config(player.player_id)
        # generic args
        generic_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning" if self.logger.isEnabledFor(logging.DEBUG) else "quiet",
            "-ignore_unknown",
        ]
        # input args
        input_args = [
            "-f",
            input_format.content_type.value,
            "-ac",
            str(input_format.channels),
            "-channel_layout",
            "mono" if input_format.channels == 1 else "stereo",
            "-ar",
            str(input_format.sample_rate),
            "-i",
            "-",
        ]
        input_args += ["-metadata", 'title="Music Assistant"']
        # select output args
        if output_format.content_type == ContentType.FLAC:
            # set compression level to 0 to prevent issues with cast players
            output_args = ["-f", "flac", "-compression_level", "0"]
        elif output_format.content_type == ContentType.AAC:
            output_args = ["-f", "adts", "-c:a", "aac", "-b:a", "320k"]
        elif output_format.content_type == ContentType.MP3:
            output_args = ["-f", "mp3", "-c:a", "mp3", "-b:a", "320k"]
        else:
            output_args = ["-f", output_format.content_type.value]

        # append channels
        output_args += ["-ac", str(output_format.channels)]
        # append sample rate (if codec is lossless)
        if output_format.content_type.is_lossless():
            output_args += ["-ar", str(output_format.sample_rate)]
        # append output = pipe
        output_args += ["-"]

        # collect extra and filter args
        # TODO: add convolution/DSP/roomcorrections here!
        extra_args = []
        filter_params = []

        # the below is a very basic 3-band equalizer,
        # this could be a lot more sophisticated at some point
        if eq_bass := player_conf.get_value(CONF_EQ_BASS):
            filter_params.append(f"equalizer=frequency=100:width=200:width_type=h:gain={eq_bass}")
        if eq_mid := player_conf.get_value(CONF_EQ_MID):
            filter_params.append(f"equalizer=frequency=900:width=1800:width_type=h:gain={eq_mid}")
        if eq_treble := player_conf.get_value(CONF_EQ_TREBLE):
            filter_params.append(
                f"equalizer=frequency=9000:width=18000:width_type=h:gain={eq_treble}"
            )
        # handle output mixing only left or right
        conf_channels = player_conf.get_value(CONF_OUTPUT_CHANNELS)
        if conf_channels == "left":
            filter_params.append("pan=mono|c0=FL")
        elif conf_channels == "right":
            filter_params.append("pan=mono|c0=FR")

        if filter_params:
            extra_args += ["-af", ",".join(filter_params)]

        return generic_args + input_args + extra_args + output_args

    def _log_request(self, request: web.Request) -> None:
        """Log request."""
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        self.logger.debug(
            "Got %s request to %s from %s\nheaders: %s\n",
            request.method,
            request.path,
            request.remote,
            request.headers,
        )

    async def _get_output_format(
        self,
        output_format_str: str,
        queue_player: Player,
        default_sample_rate: int,
        default_bit_depth: int,
    ) -> AudioFormat:
        """Parse (player specific) output format details for given format string."""
        content_type = ContentType.try_parse(output_format_str)
        if content_type.is_pcm() or content_type == ContentType.WAV:
            # parse pcm details from format string
            output_sample_rate, output_bit_depth, output_channels = parse_pcm_info(
                output_format_str
            )
            if content_type == ContentType.PCM:
                # resolve generic pcm type
                content_type = ContentType.from_bit_depth(output_bit_depth)

        else:
            output_sample_rate = min(default_sample_rate, queue_player.max_sample_rate)
            player_max_bit_depth = 32 if queue_player.supports_24bit else 16
            output_bit_depth = min(default_bit_depth, player_max_bit_depth)
            output_channels_str = await self.mass.config.get_player_config_value(
                queue_player.player_id, CONF_OUTPUT_CHANNELS
            )
            output_channels = 1 if output_channels_str != "stereo" else 2
        return AudioFormat(
            content_type=content_type,
            sample_rate=output_sample_rate,
            bit_depth=output_bit_depth,
            channels=output_channels,
            output_format_str=output_format_str,
        )
