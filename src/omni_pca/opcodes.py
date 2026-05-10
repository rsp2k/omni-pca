"""Omni-Link II protocol opcode and enum definitions.

Byte-exact mirrors of the HAI_Shared C# enums. Used by both the v1 (legacy
serial / older firmware) and v2 (modern TCP) message paths.

References:
    enuOmniLinkPacketType.cs (lines 1-17)
    enuOmniLinkMessageType.cs (lines 1-102)
    enuOmniLink2MessageType.cs (lines 1-83)
    enuOmniLinkConnectionType.cs (lines 1-10)
    enuOmniLinkProtocol.cs (lines 1-7)
"""

from __future__ import annotations

from enum import IntEnum


class PacketType(IntEnum):
    """Outer-frame packet type (the third byte of the 4-byte header)."""

    NoMessage = 0
    ClientRequestNewSession = 1
    ControllerAckNewSession = 2
    ClientRequestSecureSession = 3
    ControllerAckSecureSession = 4
    ClientSessionTerminated = 5
    ControllerSessionTerminated = 6
    ControllerCannotStartNewSession = 7
    OmniLinkMessage = 16
    OmniLinkUnencryptedMessage = 17
    OmniLink2Message = 32
    OmniLink2UnencryptedMessage = 33


class OmniLinkMessageType(IntEnum):
    """Inner-message opcode for protocol v1 (legacy)."""

    Invalid = 0
    DownloadSetup = 1
    SetupData = 2
    EOD = 3
    UploadSetup = 4
    Ack = 5
    Nak = 6
    DownloadPrograms = 7
    ProgramData = 8
    UploadPrograms = 9
    DownloadNames = 10
    NameData = 11
    UploadNames = 12
    UploadEventLog = 13
    EventLogData = 14
    Command = 15
    SetTime = 16
    RequestSystemInformation = 17
    SystemInformation = 18
    RequestSystemStatus = 19
    SystemStatus = 20
    RequestZoneStatus = 21
    ZoneStatus = 22
    RequestUnitStatus = 23
    UnitStatus = 24
    RequestAuxiliaryStatus = 25
    AuxiliaryStatus = 26
    DownloadVoice = 27
    VoiceData = 28
    UploadVoice = 29
    RequestThermostatStatus = 30
    ThermostatStatus = 31
    Login = 32
    Logout = 33
    RequestSystemEvents = 34
    SystemEvents = 35
    RequestMessageStatus = 36
    MessageStatus = 37
    RequestValidateCode = 38
    ValidateCode = 39
    RequestStatusSummary = 40
    StatusSummary = 41
    RequestAudioZoneStatus = 49
    AudioZoneStatus = 50
    RequestAudioSourceStatus = 51
    AudioSourceStatus = 52
    RequestExtSecurityStatus = 53
    ExtSecurityStatus = 54
    CmdExtSecurity = 55
    EraseFirmware = 56
    RequestFirmwareEraseStatus = 57
    FirmwareEraseStatus = 58
    DownloadFirmwareData = 59
    ControllerRestart = 60
    RequestUserSettingStatus = 61
    UserSettingStatus = 62
    RequestZoneExtendedStatus = 63
    ZoneExtendedStatus = 64
    RequestUnitExtendedStatus = 65
    UnitExtendedStatus = 66
    RequestAuxiliaryExtendedStatus = 67
    AuxiliaryExtendedStatus = 68
    RequestThermostatExtendedStatus = 69
    ThermostatExtendedStatus = 70
    RequestAudioZoneExtendedStatus = 71
    AudioZoneExtendedStatus = 72
    RequestUserSettingExtendedStatus = 73
    UserSettingExtendedStatus = 74
    UploadUserSetup2 = 75
    DownloadUserSetup2 = 76
    UploadInstallerSetup2 = 77
    DownloadInstallerSetup2 = 78
    RequestAccessControlReaderStatus = 79
    AccessControlReaderStatus = 80
    RequestAccessControlLockStatus = 81
    AccessControlLockStatus = 82
    ConfigureUPBModule = 84
    RequestUPBConfigureStatus = 85
    UPBConfigureStatus = 86
    UploadInstallerSetup3 = 87
    DownloadInstallerSetup3 = 88
    UploadInstallerSetup4 = 89
    DownloadInstallerSetup4 = 90
    ConfigureZigBeeModule = 91
    RequestZigBeeConfigureStatus = 92
    ZigBeeConfigureStatus = 93
    WriteSsData = 94
    ReadSsData = 95
    SsData = 96
    EnterPassThrough = 97
    ExitPassThrough = 98
    PassThroughData = 99
    PassThroughResponse = 100
    TouchscreenRestart = 101
    ExecuteSecurityCommand = 102
    ExecuteSecurityCommandResponse = 103
    EraseFirmware4 = 104


class OmniLink2MessageType(IntEnum):
    """Inner-message opcode for protocol v2 (modern TCP)."""

    Invalid = 0
    Ack = 1
    Nak = 2
    EOD = 3
    DownloadSetup = 4
    UploadSetup = 5
    SetupData = 6
    ClearPrograms = 7
    DownloadProgram = 8
    UploadProgram = 9
    ProgramData = 10
    ClearNames = 11
    DownloadNames = 12
    UploadNames = 13
    NameData = 14
    ClearVoices = 15
    DownloadVoices = 16
    UploadVoices = 17
    VoiceData = 18
    SetTime = 19
    Command = 20
    EnableNotifications = 21
    RequestSystemInformation = 22
    SystemInformation = 23
    RequestSystemStatus = 24
    SystemStatus = 25
    RequestSystemTroubles = 26
    SystemTroubles = 27
    RequestSystemFeatures = 28
    SystemFeatures = 29
    RequestCapacities = 30
    Capacities = 31
    RequestProperties = 32
    Properties = 33
    RequestStatus = 34
    Status = 35
    RequestEventLogItem = 36
    EventLogItem = 37
    RequestValidateCode = 38
    ValidateCode = 39
    RequestSystemFormats = 40
    SystemFormats = 41
    Login = 42
    Logout = 43
    ActivateKeypadEmg = 44
    RequestExtSecurityStatus = 45
    ExtSecurityStatus = 46
    CmdExtSecurity = 47
    RequestAudioSourceStatus = 48
    AudioSourceStatus = 49
    EraseFirmware = 50
    RequestFirmwareEraseStatus = 51
    FirmwareEraseStatus = 52
    DownloadFirmwareData = 53
    ControllerRestart = 54
    SystemEvents = 55
    RequestZoneReady = 56
    ZoneReadyStatus = 57
    RequestExtendedStatus = 58
    ExtendedStatus = 59
    AcknowledgeAlerts = 60
    ConfigureUPBModule = 61
    RequestUPBConfigureStatus = 62
    UPBConfigureStatus = 63
    ConfigureZigBeeModule = 64
    RequestZigBeeConfigureStatus = 65
    ZigBeeConfigureStatus = 66
    WriteSsData = 67
    ReadSsData = 68
    SsData = 69
    EnterPassThrough = 70
    ExitPassThrough = 71
    PassThroughData = 72
    PassThroughResponse = 73
    ExecuteSecurityCommand = 74
    ExecuteSecurityCommandResponse = 75
    TouchscreenRestart = 77
    EraseFirmware4 = 80


class ConnectionType(IntEnum):
    """Connection-medium discriminator (controller-side)."""

    NONE = 0
    Modem = 1
    Serial = 2
    Network_UDP = 3
    Network_TCP = 4


class ProtocolVersion(IntEnum):
    """Omni-Link wire protocol generation."""

    V1 = 0
    V2 = 1
