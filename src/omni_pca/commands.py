"""Command (opcode 20) and ExecuteSecurityCommand (opcode 74) primitives.

This module pins down the exact byte values the panel expects in the
*first byte* of a Command (opcode 20) payload, plus the failure-mode
exception used by the typed methods on :class:`omni_pca.client.OmniClient`.

Naming note: there is no standalone ``enuCommand`` enum in HAI_Shared —
the C# code uses :class:`enuUnitCommand` (file
``decompiled/project/HAI_Shared/enuUnitCommand.cs``) for *every* Command
opcode regardless of object type, even though the name suggests it's
unit-only. We mirror that single enum here under the cleaner name
:class:`Command`. Every member cites the line in ``enuUnitCommand.cs``
where its byte value is defined.

The rich Command (opcode 20) wire format (from
``clsOL2MsgCommand.cs:5-57``) is:

    payload[0]  = command byte (this enum)
    payload[1]  = parameter1 (single byte, e.g. brightness, mode value)
    payload[2]  = parameter2 high byte (BE u16)
    payload[3]  = parameter2 low  byte

Parameter2 is almost always the **object number** (unit#, zone#,
thermostat#, message#, button#, scene#). Parameter1 carries whatever the
specific command needs (level, mode, set-point, etc.) — see the per-
member doc-comments below for the mapping.

The ExecuteSecurityCommand (opcode 74) wire format (from
``clsOL2MsgExecuteSecurityCommand.cs:5-90``) is:

    payload[0]  = area number (1-based)
    payload[1]  = security mode byte (enuSecurityMode, raw 0-7)
    payload[2]  = code digit 1  (thousands place, 0-9)
    payload[3]  = code digit 2  (hundreds  place, 0-9)
    payload[4]  = code digit 3  (tens      place, 0-9)
    payload[5]  = code digit 4  (ones      place, 0-9)

The reply (ExecuteSecurityCommandResponse, opcode 75) carries a single
status byte at ``payload[0]`` whose values are listed in
``enuSecurityCommnadResponse.cs`` — :class:`SecurityCommandResponse`
mirrors that enum.

Cross-references (HAI OmniPro II Owner's Manual):
    Chapter "CONTROL" (pca-re/docs/owner_manual/05_CONTROL/) documents
        the user-facing keypad keys that map to these commands —
        e.g. UNIT_ON/OFF + UNIT_LEVEL are what a homeowner triggers via
        the "Control → 1 (Unit)" menu, SHOW_MESSAGE_WITH_BEEP is
        invoked from "Control → Message → Show".
    Chapter "Scene Commands" (06_Scene_Commands/) covers
        COMPOSE_SCENE and the per-room scene-recall path.
    Chapter "SECURITY SYSTEM OPERATION" (03_SECURITY_SYSTEM_OPERATION/)
        documents what each SecurityMode byte (0-6) means at the user
        level — the arming menu, entry/exit-delay semantics, and which
        zones each mode arms.
"""

from __future__ import annotations

from enum import IntEnum

from .connection import ProtocolError


class Command(IntEnum):
    """OMNI command codes used as ``payload[0]`` of a Command (opcode 20).

    Every member's value is sourced from
    ``decompiled/project/HAI_Shared/enuUnitCommand.cs``; the trailing
    line-number reference points to the exact definition.
    """

    # ---- unit / lighting ------------------------------------------------
    UNIT_OFF = 0  # enuUnitCommand.Off, line 5
    UNIT_ON = 1  # enuUnitCommand.On, line 6
    ALL_OFF = 2  # enuUnitCommand.AllOff, line 7 (alias: All=2 line 8)
    ALL_ON = 3  # enuUnitCommand.AllOn, line 9
    BYPASS_ZONE = 4  # enuUnitCommand.Bypass, line 10
    RESTORE_ZONE = 5  # enuUnitCommand.Restore, line 11
    RESTORE_ALL_ZONES = 6  # enuUnitCommand.RestoreAll, line 12
    EXECUTE_BUTTON = 7  # enuUnitCommand.Button, line 13
    ENERGY = 8  # enuUnitCommand.Energy, line 14
    UNIT_LEVEL = 9  # enuUnitCommand.Level, line 15 (param1 = 0..100 %)
    UNIT_DECREMENT_COUNTER = 10  # enuUnitCommand.Dec, line 16
    UNIT_INCREMENT_COUNTER = 11  # enuUnitCommand.Inc, line 17
    UNIT_SET_COUNTER = 12  # enuUnitCommand.Set, line 18
    UNIT_RAMP = 13  # enuUnitCommand.Ramp, line 19
    COMPOSE_SCENE = 14  # enuUnitCommand.Compose, line 20
    UPB_STATUS_REQUEST = 15  # enuUnitCommand.UPBStatus, line 21
    DIM_STEP = 16  # enuUnitCommand.Dim, line 22 (param1 = step)
    DIM_1 = 17  # enuUnitCommand.Dim1, line 23
    DIM_2 = 18  # enuUnitCommand.Dim2, line 24
    DIM_3 = 19  # enuUnitCommand.Dim3, line 25
    DIM_4 = 20  # enuUnitCommand.Dim4, line 26
    DIM_5 = 21  # enuUnitCommand.Dim5, line 27
    DIM_6 = 22  # enuUnitCommand.Dim6, line 28
    DIM_7 = 23  # enuUnitCommand.Dim7, line 29
    DIM_8 = 24  # enuUnitCommand.Dim8, line 30
    DIM_9 = 25  # enuUnitCommand.Dim9, line 31
    UPB_BLINK = 26  # enuUnitCommand.UPBBlink, line 32
    UPB_BLINK_OFF = 27  # enuUnitCommand.UPBBlinkOff, line 33
    UPB_LINK_OFF = 28  # enuUnitCommand.UPBLinkOff, line 34
    UPB_LINK_ON = 29  # enuUnitCommand.UPBLinkOn, line 35
    UPB_LINK_SET = 30  # enuUnitCommand.UPBLinkSet, line 36
    UPB_LINK_FADE_STOP = 31  # enuUnitCommand.UPBLinkFadeStop, line 37
    BRIGHT_STEP = 32  # enuUnitCommand.Bright, line 38 (param1 = step)
    BRIGHT_1 = 33  # enuUnitCommand.Bright1, line 39
    BRIGHT_2 = 34  # enuUnitCommand.Bright2, line 40
    BRIGHT_3 = 35  # enuUnitCommand.Bright3, line 41
    BRIGHT_4 = 36  # enuUnitCommand.Bright4, line 42
    BRIGHT_5 = 37  # enuUnitCommand.Bright5, line 43
    BRIGHT_6 = 38  # enuUnitCommand.Bright6, line 44
    BRIGHT_7 = 39  # enuUnitCommand.Bright7, line 45
    BRIGHT_8 = 40  # enuUnitCommand.Bright8, line 46
    BRIGHT_9 = 41  # enuUnitCommand.Bright9, line 47
    CENTRALITE_SCENE_OFF = 42  # enuUnitCommand.CentraLiteSceneOff, line 48
    CENTRALITE_SCENE_ON = 43  # enuUnitCommand.CentraLiteSceneOn, line 49
    UPB_LED_OFF = 44  # enuUnitCommand.UPBLEDOff, line 50
    UPB_LED_ON = 45  # enuUnitCommand.UPBLEDOn, line 51
    RADIO_RA_PHANTOM_OFF = 46  # enuUnitCommand.RadioRAPhantomOff, line 52
    RADIO_RA_PHANTOM_ON = 47  # enuUnitCommand.RadioRAPhantomOn, line 53

    # ---- security (alternative path; preferred path is opcode 74) ------
    # When sent through a Command (opcode 20), parameter1 carries the user
    # code index (1-based) and parameter2 carries the area number. The
    # panel honours these only if the code is enabled for the area.
    SECURITY_OFF = 48  # enuUnitCommand.SecurityOff, line 55 (alias Security=48 line 54)
    SECURITY_DAY = 49  # enuUnitCommand.SecurityDay, line 56
    SECURITY_NIGHT = 50  # enuUnitCommand.SecurityNight, line 57
    SECURITY_AWAY = 51  # enuUnitCommand.SecurityAway, line 58
    SECURITY_VACATION = 52  # enuUnitCommand.SecurityVac, line 59
    SECURITY_DAY_INSTANT = 53  # enuUnitCommand.SecurityDyi, line 60
    SECURITY_NIGHT_DELAYED = 54  # enuUnitCommand.SecurityNtd, line 61
    SECURITY_ANY_CHANGE = 55  # enuUnitCommand.SecurityAny, line 62
    SECURITY_ARMING_DAY = 57  # enuUnitCommand.SecurityArmingDay, line 63
    SECURITY_ARMING_NIGHT = 58  # enuUnitCommand.SecurityArmingNight, line 64
    SECURITY_ARMING_AWAY = 59  # enuUnitCommand.SecurityArmingAway, line 65
    SECURITY_ARMING_VACATION = 60  # enuUnitCommand.SecurityArmingVacation, line 66
    SECURITY_ARMING_DAY_INSTANT = 61  # enuUnitCommand.SecurityArmingDayInst, line 67
    SECURITY_ARMING_NIGHT_DELAYED = 62  # enuUnitCommand.SecurityArmingNightDelay, line 68

    # ---- energy (HMS) --------------------------------------------------
    ENERGY_OFF = 64  # enuUnitCommand.Eof, line 69
    ENERGY_ON = 65  # enuUnitCommand.Eon, line 70

    # ---- thermostat ---------------------------------------------------
    SET_THERMOSTAT_HEAT_SETPOINT = 66  # enuUnitCommand.SetLowSetPt, line 71
    SET_THERMOSTAT_COOL_SETPOINT = 67  # enuUnitCommand.SetHighSetPt, line 72
    SET_THERMOSTAT_SYSTEM_MODE = 68  # enuUnitCommand.Mode, line 73
    SET_THERMOSTAT_FAN_MODE = 69  # enuUnitCommand.Fan, line 74
    SET_THERMOSTAT_HOLD_MODE = 70  # enuUnitCommand.Hold, line 75
    THERMOSTAT_INC_DEC_LO = 71  # enuUnitCommand.IncDecLo, line 76
    THERMOSTAT_INC_DEC_HI = 72  # enuUnitCommand.IncDecHi, line 77
    SET_THERMOSTAT_HUMIDIFY_SETPOINT = 73  # enuUnitCommand.SetHumidifySetPt, line 78
    SET_THERMOSTAT_DEHUMIDIFY_SETPOINT = 74  # enuUnitCommand.SetDeHumidifySetPt, line 79

    # ---- panel display messages ---------------------------------------
    SHOW_MESSAGE_WITH_BEEP = 80  # enuUnitCommand.ShowMsgWBeep, line 81 (alias FirstMsgCmd=80 line 80)
    LOG_MESSAGE = 81  # enuUnitCommand.LogMsg, line 82
    CLEAR_MESSAGE = 82  # enuUnitCommand.ClearMsg, line 83
    SAY_MESSAGE = 83  # enuUnitCommand.SayMsg, line 84
    PHONE_MESSAGE = 84  # enuUnitCommand.PhoneMsg, line 85
    SEND_MESSAGE = 85  # enuUnitCommand.SendMsg, line 86
    SHOW_MESSAGE_NO_BEEP = 86  # enuUnitCommand.ShowMsgNoBeep, line 87
    EMAIL_MESSAGE = 87  # enuUnitCommand.EMailMsg, line 88 (alias LastMsgCmd=87 line 89)

    # ---- scenes / misc -----------------------------------------------
    SCENE_OFF = 96  # enuUnitCommand.SceneOff, line 90
    SCENE_ON = 97  # enuUnitCommand.SceneOn, line 91
    SCENE_SET = 98  # enuUnitCommand.SceneSet, line 92
    TOGGLE = 99  # enuUnitCommand.Toggle, line 93
    SHOW_VIDEO = 100  # enuUnitCommand.ShowVideo, line 94
    TIMED_LEVEL = 101  # enuUnitCommand.TimedLevel, line 95
    CONSOLE_BEEP = 102  # enuUnitCommand.ConsoleBeep, line 96
    BEEP = 103  # enuUnitCommand.Beep, line 97
    EXECUTE_PROGRAM = 104  # enuUnitCommand.UserSetting, line 98
    LOCK = 105  # enuUnitCommand.Lock, line 99
    UNLOCK = 106  # enuUnitCommand.Unlock, line 100
    LUTRON_HOMEWORKS_KEYPAD = 107  # enuUnitCommand.LutronHomeWorksKeypadButtonPress, line 101
    CLIPSAL_C_BUS_SCENE = 108  # enuUnitCommand.Clipsal_C_Bus_Scene, line 102
    RADIO_RA2_PHANTOM = 109  # enuUnitCommand.RadioRA2Phantom, line 103
    STOP = 110  # enuUnitCommand.Stop, line 104

    # ---- audio --------------------------------------------------------
    AUDIO_ZONE = 112  # enuUnitCommand.AudioZone, line 105
    AUDIO_VOLUME = 113  # enuUnitCommand.AudioVolume, line 106
    AUDIO_SOURCE = 114  # enuUnitCommand.AudioSource, line 107
    AUDIO_KEY_PRESS = 115  # enuUnitCommand.AudioKeyPress, line 108


class SecurityCommandResponse(IntEnum):
    """Status byte returned in an ExecuteSecurityCommandResponse (opcode 75).

    Source: ``decompiled/project/HAI_Shared/enuSecurityCommnadResponse.cs``
    (typo in the C# enum name preserved here for grep parity).
    """

    SUCCESS = 0  # line 5
    INVALID_CODE = 1  # line 6
    INVALID_SECURITY_MODE = 2  # line 7
    INVALID_AREA = 3  # line 8
    ZONES_NOT_READY = 4  # line 9
    INSTALLER_RESTORE_NEEDED = 5  # line 10
    CODE_LOCKED_OUT = 6  # line 11
    INVALID = 0xFF  # line 12


class CommandFailedError(ProtocolError):
    """A command opcode was Nak'd by the panel, or returned a structured
    failure code (e.g. the Security command response carries one of the
    :class:`SecurityCommandResponse` values).

    The ``failure_code`` attribute is set when the panel returned an
    ExecuteSecurityCommandResponse with a non-zero status byte; it's
    ``None`` for plain Nak replies that carry no further detail.
    """

    def __init__(
        self,
        message: str,
        *,
        failure_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
