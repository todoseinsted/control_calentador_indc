from __future__ import annotations

import sys
import types
import unittest


# Las pruebas usan controladores falsos y no abren puertos. Estos modulos
# minimos permiten importar el codigo en una PC de desarrollo sin pyserial ni
# minimalmodbus instalados.
if "minimalmodbus" not in sys.modules:
    minimalmodbus = types.ModuleType("minimalmodbus")
    minimalmodbus.ModbusException = type("ModbusException", (Exception,), {})
    minimalmodbus.MODE_RTU = "rtu"
    minimalmodbus.Instrument = object
    sys.modules["minimalmodbus"] = minimalmodbus

if "serial" not in sys.modules:
    serial = types.ModuleType("serial")
    serial.SerialException = type("SerialException", (Exception,), {})
    serial.PARITY_NONE = "N"
    sys.modules["serial"] = serial

from control_calentador_plc import HeaterStatus
from pfc_controller import (
    PFCController,
    PFCStatus,
    build_control_payload,
    health_has_tx_error,
)
from sistema_pfc_calentador import CombinedController, power_level_percent


class FakePFC:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.current = PFCStatus(communication_ok=True)
        self.service_error: Exception | None = None

    def start(self, voltage: float, current: float) -> None:
        self.events.append("pfc.start")
        self.current.requested_on = True
        self.current.voltage_v = voltage
        self.current.current_a = current
        self.current.communication_ok = True

    def stop(self) -> None:
        self.events.append("pfc.stop")
        self.current.requested_on = False
        self.current.voltage_v = 0.0
        self.current.current_a = 0.0

    def recover(self) -> None:
        self.events.append("pfc.recover")

    def service(self) -> None:
        if self.service_error:
            raise self.service_error

    def status(self) -> PFCStatus:
        return PFCStatus(**self.current.to_dict())

    def close(self) -> None:
        self.events.append("pfc.close")


class FakeHeater:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.current = HeaterStatus(online=True, switch=0, alarm=0, power_raw=0)
        self.requested_on = False

    def turn_on(self) -> None:
        self.events.append("heater.on")
        self.requested_on = True
        self.current.switch = 1
        self.current.requested_on = True

    def turn_off(self) -> None:
        self.events.append("heater.off")
        self.requested_on = False
        self.current.switch = 0
        self.current.requested_on = False

    def set_power(self, power: int) -> None:
        self.events.append(f"heater.power:{power}")
        self.current.power_raw = power

    def keep_alive_if_due(self) -> bool:
        return False

    def read_status(self) -> HeaterStatus:
        return HeaterStatus(**vars(self.current))

    def close(self) -> None:
        self.events.append("heater.close")


class FakeMCP:
    def __init__(self, health_sequence: list[dict[str, int]] | None = None) -> None:
        self.events: list[str] = []
        self.payloads: list[bytes] = []
        self.health_sequence = list(health_sequence or [])

    def initialize(self) -> None:
        self.events.append("initialize")

    def send_extended(self, _arbitration_id: int, payload: bytes) -> dict[str, int]:
        self.events.append("send")
        self.payloads.append(payload)
        if self.health_sequence:
            return self.health_sequence.pop(0)
        return {"tx_ctrl": 0, "eflg": 0, "tec": 0, "rec": 0}

    def close(self) -> None:
        self.events.append("close")


class CombinedControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[str] = []
        self.pfc = FakePFC(self.events)
        self.heater = FakeHeater(self.events)
        self.controller = CombinedController(
            self.pfc, self.heater, stabilization_seconds=0
        )

    def start_running(self) -> None:
        self.controller.start_system(311.0, 10.0, 115)
        self.controller.service()
        self.assertEqual(self.controller.status_payload()["system"]["mode"], "running")

    def test_start_orders_heater_off_before_pfc_and_heater_on_last(self) -> None:
        self.start_running()
        self.assertEqual(
            self.events,
            ["pfc.start", "heater.off", "heater.power:115", "heater.on"],
        )

    def test_start_pfc_keeps_heater_off(self) -> None:
        self.controller.start_pfc(311.0, 10.0)
        self.assertEqual(self.events, ["pfc.start"])
        self.assertEqual(
            self.controller.status_payload()["system"]["mode"], "pfc_on"
        )
        self.assertTrue(self.pfc.current.requested_on)
        self.assertFalse(self.heater.requested_on)

        self.controller.service()
        self.assertEqual(self.events, ["pfc.start", "heater.off"])
        self.assertEqual(
            self.controller.status_payload()["system"]["mode"], "pfc_on"
        )

    def test_start_heater_after_pfc_is_already_on(self) -> None:
        self.controller.start_pfc(311.0, 10.0)
        self.controller.start_heater(5)
        self.controller.service()
        self.assertEqual(
            self.events,
            ["pfc.start", "heater.off", "heater.power:5", "heater.on"],
        )
        self.assertEqual(
            self.controller.status_payload()["system"]["mode"], "running"
        )

    def test_stop_heater_keeps_pfc_on(self) -> None:
        self.start_running()
        self.events.clear()
        self.controller.stop_heater()
        self.assertEqual(self.events, ["heater.off"])
        self.assertTrue(self.pfc.current.requested_on)
        self.assertFalse(self.heater.requested_on)
        self.assertEqual(
            self.controller.status_payload()["system"]["mode"], "pfc_on"
        )

    def test_stop_heater_is_idempotent_when_pfc_is_off(self) -> None:
        self.controller.stop_heater()
        self.assertEqual(self.events, [])
        self.assertFalse(self.pfc.current.requested_on)
        self.assertFalse(self.heater.requested_on)

    def test_pfc_only_tolerates_unavailable_heater(self) -> None:
        self.heater.current.online = False
        self.heater.current.error = "sin respuesta"
        self.controller.start_pfc(311.0, 10.0)
        self.controller.service()
        self.assertEqual(
            self.controller.status_payload()["system"]["mode"], "pfc_on"
        )
        self.assertTrue(self.pfc.current.requested_on)

    def test_start_waits_for_heater_powered_by_pfc(self) -> None:
        self.heater.current.online = False
        self.heater.current.error = "sin alimentacion"
        self.controller.start_system(311.0, 10.0, 5)
        self.controller.service()
        self.assertEqual(self.events, ["pfc.start"])
        self.assertEqual(
            self.controller.status_payload()["system"]["mode"], "starting"
        )

        self.heater.current.online = True
        self.heater.current.error = ""
        self.controller._last_heater_poll = 0.0
        self.controller.service()
        self.assertEqual(
            self.events,
            ["pfc.start", "heater.off", "heater.power:5", "heater.on"],
        )
        self.assertEqual(
            self.controller.status_payload()["system"]["mode"], "running"
        )

    def test_stop_orders_heater_off_before_pfc_stop(self) -> None:
        self.start_running()
        self.events.clear()
        self.controller.stop_system()
        self.assertEqual(self.events, ["heater.off", "pfc.stop"])
        self.assertEqual(self.controller.status_payload()["system"]["mode"], "off")

    def test_stop_is_idempotent_when_system_is_already_off(self) -> None:
        self.controller.stop_system()
        self.assertEqual(self.events, ["pfc.stop"])
        payload = self.controller.status_payload()
        self.assertEqual(payload["system"]["mode"], "off")
        self.assertFalse(payload["system"]["message_is_error"])

    def test_alarm_stops_both_devices(self) -> None:
        self.start_running()
        self.events.clear()
        self.heater.current.alarm = 4
        self.controller._last_heater_poll = 0.0
        self.controller.service()
        self.assertEqual(self.events, ["heater.off", "pfc.stop"])
        payload = self.controller.status_payload()
        self.assertEqual(payload["system"]["mode"], "fault")
        self.assertIn("Temperatura IGBT alta", payload["system"]["message"])

    def test_heater_communication_loss_stops_both_devices(self) -> None:
        self.start_running()
        self.events.clear()
        self.heater.current.online = False
        self.heater.current.error = "timeout"
        self.controller._last_heater_poll = 0.0
        self.controller.service()
        self.assertEqual(self.events, ["heater.off", "pfc.stop"])
        self.assertEqual(self.controller.status_payload()["system"]["mode"], "fault")

    def test_pfc_service_failure_stops_both_devices(self) -> None:
        self.start_running()
        self.events.clear()
        self.pfc.service_error = RuntimeError("CAN bus-off")
        self.controller.service()
        self.assertEqual(self.events, ["heater.off", "pfc.stop"])
        self.assertEqual(self.controller.status_payload()["system"]["mode"], "fault")

    def test_power_scale_is_not_watts(self) -> None:
        self.assertEqual(power_level_percent(0), 0.0)
        self.assertEqual(power_level_percent(115), 50.0)
        self.assertEqual(power_level_percent(230), 100.0)

    def test_pfc_payload_encodes_311_volts_and_10_amps(self) -> None:
        payload = build_control_payload(311.0, 10.0, True)
        self.assertEqual(payload, bytes.fromhex("AA 00 26 0C E8 03 00 00"))

    def test_receive_overflow_without_int_is_not_a_transmit_error(self) -> None:
        self.assertFalse(
            health_has_tx_error(
                {"tx_ctrl": 0, "eflg": 0xC0, "tec": 0, "rec": 0}
            )
        )

    def test_pfc_start_reinitializes_and_sends_start(self) -> None:
        mcp = FakeMCP()
        pfc = PFCController(
            mcp=mcp, acquire_process_lock=False, recovery_interval=0
        )
        mcp.events.clear()
        mcp.payloads.clear()
        pfc.start(311.0, 10.0)
        self.assertEqual(mcp.events, ["initialize", "send"])
        self.assertEqual(mcp.payloads[-1][0], 0xAA)
        self.assertTrue(pfc.status().communication_ok)

    def test_pfc_recovers_after_two_consecutive_errors(self) -> None:
        good = {"tx_ctrl": 0, "eflg": 0, "tec": 0, "rec": 0}
        error = {"tx_ctrl": 0x10, "eflg": 0x20, "tec": 8, "rec": 0}
        mcp = FakeMCP([good, good, error, error, good])
        pfc = PFCController(
            mcp=mcp, acquire_process_lock=False, recovery_interval=0
        )
        pfc.start(311.0, 10.0)
        pfc._next_send = 0.0
        pfc.service()
        self.assertEqual(pfc.status().consecutive_errors, 1)
        pfc._next_send = 0.0
        pfc.service()
        status = pfc.status()
        self.assertEqual(status.consecutive_errors, 0)
        self.assertTrue(status.communication_ok)
        self.assertGreaterEqual(status.recoveries, 2)


if __name__ == "__main__":
    unittest.main()

