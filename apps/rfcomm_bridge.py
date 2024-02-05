# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
import asyncio
import logging
import os
from typing import Optional

import click

from bumble.colors import color
from bumble.device import Device
from bumble import core
from bumble import hci
from bumble import l2cap
from bumble import rfcomm
from bumble import sdp
from bumble import transport
from bumble import utils


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
DEFAULT_RFCOMM_UUID = "E6D55659-C8B4-4B85-96BB-B1143AF6D3AE"
DEFAULT_MTU = 4096
DEFAULT_TCP_PORT = 9544


# -----------------------------------------------------------------------------
def make_sdp_records(channel: int, uuid: str):
    return {
        0x00010001: [
            sdp.ServiceAttribute(
                sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
                sdp.DataElement.unsigned_integer_32(0x00010001),
            ),
            sdp.ServiceAttribute(
                sdp.SDP_BROWSE_GROUP_LIST_ATTRIBUTE_ID,
                sdp.DataElement.sequence(
                    [sdp.DataElement.uuid(sdp.SDP_PUBLIC_BROWSE_ROOT)]
                ),
            ),
            sdp.ServiceAttribute(
                sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
                sdp.DataElement.sequence([sdp.DataElement.uuid(core.UUID(uuid))]),
            ),
            sdp.ServiceAttribute(
                sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
                sdp.DataElement.sequence(
                    [
                        sdp.DataElement.sequence(
                            [sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID)]
                        ),
                        sdp.DataElement.sequence(
                            [
                                sdp.DataElement.uuid(core.BT_RFCOMM_PROTOCOL_ID),
                                sdp.DataElement.unsigned_integer_8(channel),
                            ]
                        ),
                    ]
                ),
            ),
        ]
    }


# -----------------------------------------------------------------------------
class ServerBridge:
    """
    RFCOMM server bridge: waits for a peer to connect an RFCOMM channel.
    The RFCOMM channel may be associated with a UUID published in an SDP service
    description, or simply be on a system-assigned channel number.
    When the connection is made, the bridge connects a TCP socket to a remote host and
    bridges the data in both directions, with flow control.
    When the RFCOMM channel is closed, the bridge disconnects the TCP socket
    and waits for a new channel to be connected.
    """

    READ_CHUNK_SIZE = 4096

    def __init__(self, channel: int, uuid: str, tcp_host: str, tcp_port: int):
        self.device: Optional[Device] = None
        self.channel = channel
        self.uuid = uuid
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.rfcomm_channel: Optional[rfcomm.DLC] = None

    async def start(self, device: Device) -> None:
        self.device = device

        # Create and register a server
        rfcomm_server = rfcomm.Server(self.device)

        # Listen for incoming DLC connections
        self.channel = rfcomm_server.listen(self.on_rfcomm_channel, self.channel)

        # Setup the SDP to advertise this channel
        self.device.sdp_service_records = make_sdp_records(self.channel, self.uuid)

        # We're ready for a connection
        self.device.on("connection", self.on_connection)
        await self.set_available(True)

        print(
            color(
                (
                    f"### Listening for RFCOMM connection on {device.public_address}, "
                    f"channel {self.channel}"
                ),
                "yellow",
            )
        )

    async def set_available(self, available: bool):
        # Become discoverable and connectable
        assert self.device
        await self.device.set_connectable(available)
        await self.device.set_discoverable(available)

    def on_connection(self, connection):
        print(color(f"@@@ Bluetooth connection: {connection}", "green"))
        connection.on("disconnection", self.on_disconnection)

        # Don't accept new connections until we're disconnected
        utils.AsyncRunner.spawn(self.set_available(False))

    def on_disconnection(self, reason: int):
        print(
            color("@@@ Bluetooth disconnection:", "red"),
            hci.HCI_Constant.error_name(reason),
        )

        # We're ready for a new connection
        utils.AsyncRunner.spawn(self.set_available(True))

    # Called when an RFCOMM channel is established
    @utils.AsyncRunner.run_in_task()
    async def on_rfcomm_channel(self, rfcomm_channel):
        print(color("*** RFCOMM channel:", "cyan"), rfcomm_channel)

        # Connect to the TCP server
        print(
            color(
                f"### Connecting to TCP {self.tcp_host}:{self.tcp_port}...",
                "yellow",
            )
        )
        try:
            reader, writer = await asyncio.open_connection(self.tcp_host, self.tcp_port)
        except OSError:
            print(color("!!! Connection failed", "red"))
            await rfcomm_channel.disconnect()
            return

        # Pipe data from RFCOMM to TCP
        def on_rfcomm_channel_closed():
            print(color("*** RFCOMM channel closed", "cyan"))
            writer.close()

        rfcomm_channel.sink = writer.write
        rfcomm_channel.on("close", on_rfcomm_channel_closed)

        # Pipe data from TCP to RFCOMM
        while True:
            try:
                data = await reader.read(self.READ_CHUNK_SIZE)

                if len(data) == 0:
                    print(color("### TCP end of stream", "yellow"))
                    if rfcomm_channel.state == rfcomm.DLC.State.CONNECTED:
                        await rfcomm_channel.disconnect()
                    return

                rfcomm_channel.write(data)
                await rfcomm_channel.drain()
            except Exception as error:
                print(f"!!! Exception: {error}")
                break

        writer.close()
        await writer.wait_closed()
        print(color("~~~ Bye bye", "magenta"))


# -----------------------------------------------------------------------------
class ClientBridge:
    """
    RFCOMM client bridge: connects to a BR/EDR device, then waits for an inbound
    TCP connection on a specified port number. When a TCP client connects, an
    RFCOMM connection to the device is established, and the data is bridged in both
    directions, with flow control.
    When the TCP connection is closed by the client, the RFCOMM channel is
    disconnected, but the connection to the device remains, ready for a new TCP client
    to connect.
    """

    READ_CHUNK_SIZE = 4096

    def __init__(self, channel: int, uuid: str, address: str, tcp_host, tcp_port):
        self.channel = channel
        self.uuid = uuid
        self.address = address
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port

    async def start(self, device):
        print(color(f"### Connecting to {self.address}...", "yellow"))
        connection = await device.connect(
            self.address, transport=core.BT_BR_EDR_TRANSPORT
        )
        print(color("### Connected", "green"))

        def on_disconnection(reason):
            print(
                color("@@@ Bluetooth disconnection:", "red"),
                hci.HCI_Constant.error_name(reason),
            )

        connection.on("disconnection", on_disconnection)

        # Resolve the channel number from the UUID if needed
        if self.channel == 0:
            self.channel = await rfcomm.find_rfcomm_channel_with_uuid(
                connection, self.uuid
            )
            print(color(f"@@@ Found RFCOMM channel {self.channel}", "cyan"))

        # Called when a TCP connection is established
        async def on_tcp_connection(reader, writer):
            peer_name = writer.get_extra_info("peer_name")
            print(color(f"<<< TCP connection from {peer_name}", "magenta"))

            # Connect a new RFCOMM channel
            print(color(f'### Opening session for channel {self.channel}...', 'yellow'))
            rfcomm_client = rfcomm.Client(connection)
            rfcomm_mux = await rfcomm_client.start()
            try:
                rfcomm_channel = await rfcomm_mux.open_dlc(self.channel)
                print(color(f'### RFCOMM channel open: {rfcomm_channel}', "yellow"))
            except core.ConnectionError as error:
                print(color(f'!!! RFCOMM open failed: {error}', 'red'))
                await rfcomm_mux.disconnect()
                return

            # Pipe data from RFCOMM to TCP
            def on_rfcomm_channel_closed():
                print(color("*** RFCOMM channel closed", "cyan"))
                writer.close()

            rfcomm_channel.on("close", on_rfcomm_channel_closed)
            rfcomm_channel.sink = writer.write

            # Pipe data from TCP to RFCOMM
            while True:
                try:
                    data = await reader.read(self.READ_CHUNK_SIZE)

                    if len(data) == 0:
                        print(color("### TCP end of stream", "yellow"))
                        if rfcomm_channel.state == rfcomm.DLC.State.CONNECTED:
                            await rfcomm_channel.disconnect()
                        return

                    rfcomm_channel.write(data)
                    await rfcomm_channel.drain()
                except Exception as error:
                    print(f"!!! Exception: {error}")
                    break

            writer.close()
            await writer.wait_closed()
            print(color("~~~ Bye bye", "magenta"))

        await asyncio.start_server(
            on_tcp_connection,
            host=self.tcp_host if self.tcp_host != "_" else None,
            port=self.tcp_port,
        )
        print(
            color(
                f"### Listening for TCP connections on port {self.tcp_port}", "magenta"
            )
        )


# -----------------------------------------------------------------------------
async def run(device_config, hci_transport, bridge):
    print("<<< connecting to HCI...")
    async with await transport.open_transport_or_link(hci_transport) as (
        hci_source,
        hci_sink,
    ):
        print("<<< connected")

        device = Device.from_config_file_with_hci(device_config, hci_source, hci_sink)
        device.classic_enabled = True

        # Let's go
        await device.power_on()
        await bridge.start(device)

        # Wait until the transport terminates
        await hci_source.wait_for_termination()


# -----------------------------------------------------------------------------
@click.group()
@click.pass_context
@click.option("--device-config", help="Device configuration file", required=True)
@click.option("--hci-transport", help="HCI transport", required=True)
@click.option("--channel", help="RFCOMM channel number", type=int, default=0)
@click.option("--uuid", help="UUID for the RFCOMM channel", default=DEFAULT_RFCOMM_UUID)
def cli(
    context,
    device_config,
    hci_transport,
    channel,
    uuid,
):
    context.ensure_object(dict)
    context.obj["device_config"] = device_config
    context.obj["hci_transport"] = hci_transport
    context.obj["channel"] = channel
    context.obj["uuid"] = uuid


# -----------------------------------------------------------------------------
@cli.command()
@click.pass_context
@click.option("--tcp-host", help="TCP host", default="localhost")
@click.option("--tcp-port", help="TCP port", default=DEFAULT_TCP_PORT)
def server(context, tcp_host, tcp_port):
    bridge = ServerBridge(
        context.obj["channel"],
        context.obj["uuid"],
        tcp_host,
        tcp_port,
    )
    asyncio.run(run(context.obj["device_config"], context.obj["hci_transport"], bridge))


# -----------------------------------------------------------------------------
@cli.command()
@click.pass_context
@click.argument("bluetooth-address")
@click.option("--tcp-host", help="TCP host", default="_")
@click.option("--tcp-port", help="TCP port", default=DEFAULT_TCP_PORT)
def client(context, bluetooth_address, tcp_host, tcp_port):
    bridge = ClientBridge(
        context.obj["channel"],
        context.obj["uuid"],
        bluetooth_address,
        tcp_host,
        tcp_port,
    )
    asyncio.run(run(context.obj["device_config"], context.obj["hci_transport"], bridge))


# -----------------------------------------------------------------------------
logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())
if __name__ == "__main__":
    cli(obj={})  # pylint: disable=no-value-for-parameter
