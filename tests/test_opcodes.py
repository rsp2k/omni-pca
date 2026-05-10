"""Pin enum byte values against the C# source of truth."""

from __future__ import annotations

from omni_pca.opcodes import (
    ConnectionType,
    OmniLink2MessageType,
    OmniLinkMessageType,
    PacketType,
    ProtocolVersion,
)


def test_packet_type_values() -> None:
    assert PacketType.NoMessage == 0
    assert PacketType.ClientRequestNewSession == 1
    assert PacketType.ControllerAckNewSession == 2
    assert PacketType.ClientRequestSecureSession == 3
    assert PacketType.ControllerAckSecureSession == 4
    assert PacketType.OmniLinkMessage == 16
    assert PacketType.OmniLinkUnencryptedMessage == 17
    assert PacketType.OmniLink2Message == 32
    assert PacketType.OmniLink2UnencryptedMessage == 33


def test_v2_message_type_values() -> None:
    assert OmniLink2MessageType.Ack == 1
    assert OmniLink2MessageType.Nak == 2
    assert OmniLink2MessageType.Command == 20
    assert OmniLink2MessageType.RequestSystemInformation == 22
    assert OmniLink2MessageType.SystemInformation == 23
    assert OmniLink2MessageType.Login == 42
    assert OmniLink2MessageType.RequestExtendedStatus == 58
    assert OmniLink2MessageType.ExtendedStatus == 59
    assert OmniLink2MessageType.EraseFirmware4 == 80


def test_v1_message_type_values() -> None:
    assert OmniLinkMessageType.Ack == 5
    assert OmniLinkMessageType.Nak == 6
    assert OmniLinkMessageType.Command == 15
    assert OmniLinkMessageType.RequestSystemInformation == 17
    assert OmniLinkMessageType.SystemInformation == 18
    assert OmniLinkMessageType.Login == 32
    assert OmniLinkMessageType.Logout == 33
    assert OmniLinkMessageType.EraseFirmware4 == 104


def test_connection_type_values() -> None:
    assert ConnectionType.NONE == 0
    assert ConnectionType.Modem == 1
    assert ConnectionType.Serial == 2
    assert ConnectionType.Network_UDP == 3
    assert ConnectionType.Network_TCP == 4


def test_protocol_version_values() -> None:
    assert ProtocolVersion.V1 == 0
    assert ProtocolVersion.V2 == 1
