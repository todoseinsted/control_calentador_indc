#!/usr/bin/env python3
"""Control reutilizable del PFC TonHe mediante MCP2515 conectado por SPI."""

from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

try:
    import spidev
except ImportError:  # Permite ejecutar las pruebas en equipos sin SPI.
    spidev = None  # type: ignore[assignment]


CONTROL_CAN_ID = 0x080601A0
SEND_PERIOD_SECONDS = 0.5
DEFAULT_RECOVERY_INTERVAL_SECONDS = 5.0
MAX_CONSECUTIVE_TX_ERRORS = 2

MIN_START_VOLTAGE = 150.0
MAX_START_VOLTAGE = 1000.0
MIN_START_CURRENT = 2.0
MAX_START_CURRENT = 120.0
MAX_OUTPUT_POWER_W = 30_000.0


class PFCError(RuntimeError):
    """Error del controlador SPI/CAN del PFC."""


class CANTransmissionError(PFCError):
    """La trama CAN no pudo transmitirse correctamente."""


@dataclass
class PFCStatus:
    requested_on: bool = False
    voltage_v: float = 0.0
    current_a: float = 0.0
    communication_ok: bool = False
    frames_sent: int = 0
    recoveries: int = 0
    consecutive_errors: int = 0
    tx_ctrl: int = 0
    eflg: int = 0
    tec: int = 0
    rec: int = 0
    last_error: str = "Todavia no se inicializo el PFC"
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_start_values(voltage: float, current: float) -> None:
    if not MIN_START_VOLTAGE <= voltage <= MAX_START_VOLTAGE:
        raise ValueError(
            f"Tension fuera de rango ({MIN_START_VOLTAGE:g}.."
            f"{MAX_START_VOLTAGE:g} V)"
        )
    if not MIN_START_CURRENT <= current <= MAX_START_CURRENT:
        raise ValueError(
            f"Corriente fuera de rango ({MIN_START_CURRENT:g}.."
            f"{MAX_START_CURRENT:g} A)"
        )
    if voltage * current > MAX_OUTPUT_POWER_W:
        raise ValueError(
            f"La consigna excede {MAX_OUTPUT_POWER_W / 1000:g} kW "
            f"({voltage * current / 1000:.2f} kW solicitados)"
        )


def build_control_payload(voltage: float, current: float, enabled: bool) -> bytes:
    if enabled:
        validate_start_values(voltage, current)
    else:
        voltage = 0.0
        current = 0.0

    return bytes([0xAA if enabled else 0x55, 0x00]) + struct.pack(
        "<HHBB", round(voltage * 10), round(current * 100), 0x00, 0x00
    )


def health_has_tx_error(health: dict[str, int]) -> bool:
    return bool(
        health.get("tx_ctrl", 0) & 0x70
        or health.get("eflg", 0) & 0x3F
        or health.get("tec", 0) > 0
    )


class MCP2515:
    INSTRUCTION_RESET = 0xC0
    INSTRUCTION_READ = 0x03
    INSTRUCTION_WRITE = 0x02
    INSTRUCTION_BIT_MODIFY = 0x05
    INSTRUCTION_RTS_TX0 = 0x81

    CANSTAT = 0x0E
    CANCTRL = 0x0F
    TEC = 0x1C
    REC = 0x1D
    CNF3 = 0x28
    CNF2 = 0x29
    CNF1 = 0x2A
    CANINTE = 0x2B
    CANINTF = 0x2C
    EFLG = 0x2D
    TXB0CTRL = 0x30
    TXB0SIDH = 0x31
    RXB0CTRL = 0x60
    RXB1CTRL = 0x70

    CANCTRL_OSM = 0x08
    CNF_125K_8MHZ = (0x01, 0xB1, 0x85)

    def __init__(
        self,
        bus: int = 0,
        device: int = 1,
        spi_speed_hz: int = 500_000,
        spi_factory: Callable[[], Any] | None = None,
    ) -> None:
        if spi_factory is None:
            if spidev is None:
                raise PFCError("El modulo spidev no esta instalado")
            spi_factory = spidev.SpiDev
        self._spi = spi_factory()
        self._spi.open(bus, device)
        self._spi.max_speed_hz = spi_speed_hz
        self._spi.mode = 0
        self._spi.bits_per_word = 8
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._spi.close()
            self._closed = True

    def _transfer(self, values: Iterable[int]) -> list[int]:
        if self._closed:
            raise PFCError("El dispositivo SPI esta cerrado")
        return self._spi.xfer2(list(values))

    def reset(self) -> None:
        with self._lock:
            self._transfer([self.INSTRUCTION_RESET])
        time.sleep(0.02)

    def read_register(self, address: int) -> int:
        with self._lock:
            return self._transfer([self.INSTRUCTION_READ, address, 0x00])[2]

    def write_register(self, address: int, value: int) -> None:
        with self._lock:
            self._transfer([self.INSTRUCTION_WRITE, address, value & 0xFF])

    def write_registers(self, address: int, values: Iterable[int]) -> None:
        payload = [value & 0xFF for value in values]
        with self._lock:
            self._transfer([self.INSTRUCTION_WRITE, address] + payload)

    def initialize(self) -> None:
        self.reset()
        cnf1, cnf2, cnf3 = self.CNF_125K_8MHZ
        self.write_register(self.CNF1, cnf1)
        self.write_register(self.CNF2, cnf2)
        self.write_register(self.CNF3, cnf3)
        self.write_register(self.CANINTE, 0x00)
        self.write_register(self.CANINTF, 0x00)
        self.write_register(self.RXB0CTRL, 0x20)
        self.write_register(self.RXB1CTRL, 0x20)
        self.write_register(self.TXB0CTRL, 0x00)
        # One-shot: si falta ACK, el siguiente heartbeat hace un nuevo intento.
        self.write_register(self.CANCTRL, self.CANCTRL_OSM)
        time.sleep(0.02)

    @staticmethod
    def _encode_extended_id(arbitration_id: int) -> bytes:
        if not 0 <= arbitration_id <= 0x1FFFFFFF:
            raise ValueError("El identificador CAN extendido debe tener 29 bits")
        low = arbitration_id & 0xFFFF
        high = arbitration_id >> 16
        return bytes(
            [
                (high >> 5) & 0xFF,
                (high & 0x03) | ((high & 0x1C) << 3) | 0x08,
                (low >> 8) & 0xFF,
                low & 0xFF,
            ]
        )

    def transmission_health(self) -> dict[str, int]:
        samples: list[dict[str, int]] = []
        for _ in range(3):
            samples.append(
                {
                    "tx_ctrl": self.read_register(self.TXB0CTRL),
                    "eflg": self.read_register(self.EFLG),
                    "tec": self.read_register(self.TEC),
                    "rec": self.read_register(self.REC),
                }
            )
            time.sleep(0.001)
        return {
            "tx_ctrl": samples[0]["tx_ctrl"]
            & samples[1]["tx_ctrl"]
            & samples[2]["tx_ctrl"],
            "eflg": samples[0]["eflg"]
            & samples[1]["eflg"]
            & samples[2]["eflg"],
            "tec": sorted(sample["tec"] for sample in samples)[1],
            "rec": sorted(sample["rec"] for sample in samples)[1],
        }

    def send_extended(self, arbitration_id: int, data: bytes) -> dict[str, int]:
        if len(data) > 8:
            raise ValueError("CAN clasico admite como maximo 8 bytes")
        header = self._encode_extended_id(arbitration_id)
        self.write_register(self.TXB0CTRL, 0x00)
        self.write_registers(self.TXB0SIDH, header + bytes([len(data)]) + data)
        with self._lock:
            self._transfer([self.INSTRUCTION_RTS_TX0])
        time.sleep(0.02)
        return self.transmission_health()


class PFCController:
    """Mantiene la consigna, heartbeat y recuperacion automatica del PFC."""

    def __init__(
        self,
        bus: int = 0,
        device: int = 1,
        spi_speed_hz: int = 500_000,
        recovery_interval: float = DEFAULT_RECOVERY_INTERVAL_SECONDS,
        mcp: MCP2515 | Any | None = None,
        acquire_process_lock: bool = True,
    ) -> None:
        if recovery_interval < 0:
            raise ValueError("El intervalo de recuperacion no puede ser negativo")
        self._lock = threading.RLock()
        self._process_lock: Any | None = None
        if acquire_process_lock:
            self._acquire_process_lock()
        try:
            self._mcp = (
                mcp if mcp is not None else MCP2515(bus, device, spi_speed_hz)
            )
        except Exception:
            if self._process_lock is not None:
                self._process_lock.close()
                self._process_lock = None
            raise
        self._recovery_interval = recovery_interval
        self._status = PFCStatus()
        self._next_send = 0.0
        self._last_recovery = time.monotonic()
        self._closed = False
        try:
            self._mcp.initialize()
            self._transmit_current_command()
        except Exception:
            try:
                self._mcp.close()
            finally:
                if self._process_lock is not None:
                    self._process_lock.close()
                    self._process_lock = None
            raise

    def _acquire_process_lock(self) -> None:
        try:
            import fcntl
        except ImportError as exc:
            raise PFCError("El bloqueo del SPI requiere Linux") from exc
        lock_file = open("/tmp/control-pfc-spi.lock", "w", encoding="ascii")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise PFCError("Ya hay otra instancia controlando el PFC") from exc
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        self._process_lock = lock_file

    @property
    def requested_on(self) -> bool:
        with self._lock:
            return self._status.requested_on

    def status(self) -> PFCStatus:
        with self._lock:
            return PFCStatus(**asdict(self._status))

    def _payload(self) -> bytes:
        return build_control_payload(
            self._status.voltage_v,
            self._status.current_a,
            self._status.requested_on,
        )

    def _record_health(self, health: dict[str, int]) -> None:
        self._status.tx_ctrl = health.get("tx_ctrl", 0)
        self._status.eflg = health.get("eflg", 0)
        self._status.tec = health.get("tec", 0)
        self._status.rec = health.get("rec", 0)
        self._status.communication_ok = not health_has_tx_error(health)
        self._status.updated_at = time.time()
        if self._status.communication_ok:
            self._status.last_error = ""

    def _transmit_current_command(self) -> None:
        health = self._mcp.send_extended(CONTROL_CAN_ID, self._payload())
        self._status.frames_sent += 1
        self._record_health(health)
        self._next_send = time.monotonic() + SEND_PERIOD_SECONDS

    def _recover(self, reason: str) -> None:
        self._mcp.initialize()
        self._status.recoveries += 1
        self._status.consecutive_errors = 0
        self._last_recovery = time.monotonic()
        self._transmit_current_command()
        if not self._status.communication_ok:
            raise CANTransmissionError(
                f"La recuperacion CAN no obtuvo una transmision valida: {reason}"
            )

    def start(self, voltage: float, current: float) -> None:
        validate_start_values(voltage, current)
        with self._lock:
            self._status.voltage_v = float(voltage)
            self._status.current_a = float(current)
            self._status.requested_on = True
            try:
                self._recover("nuevo START")
            except Exception as exc:
                self._status.requested_on = False
                self._status.voltage_v = 0.0
                self._status.current_a = 0.0
                self._status.communication_ok = False
                self._status.last_error = str(exc)
                self._status.updated_at = time.time()
                raise

    def stop(self) -> None:
        with self._lock:
            self._status.requested_on = False
            self._status.voltage_v = 0.0
            self._status.current_a = 0.0
            try:
                self._recover("STOP")
            except Exception as exc:
                self._status.communication_ok = False
                self._status.last_error = str(exc)
                self._status.updated_at = time.time()
                raise

    def recover(self) -> None:
        with self._lock:
            self._recover("solicitud manual")

    def service(self, now: float | None = None) -> None:
        with self._lock:
            if self._closed:
                raise PFCError("El controlador PFC esta cerrado")
            current_time = time.monotonic() if now is None else now
            if (
                self._status.requested_on
                and self._recovery_interval > 0
                and current_time - self._last_recovery >= self._recovery_interval
            ):
                self._recover("refresco preventivo")
                return
            if current_time < self._next_send:
                return

            try:
                self._transmit_current_command()
                if self._status.communication_ok:
                    self._status.consecutive_errors = 0
                else:
                    self._status.consecutive_errors += 1
                    self._status.last_error = (
                        f"Error CAN: TXB0CTRL=0x{self._status.tx_ctrl:02X}, "
                        f"EFLG=0x{self._status.eflg:02X}, "
                        f"TEC={self._status.tec}, REC={self._status.rec}"
                    )
                    if self._status.consecutive_errors >= MAX_CONSECUTIVE_TX_ERRORS:
                        self._recover("errores consecutivos")
            except Exception as exc:
                self._status.communication_ok = False
                self._status.last_error = str(exc)
                self._status.updated_at = time.time()
                try:
                    self._recover("excepcion de comunicacion")
                except Exception as recovery_exc:
                    self._status.last_error = str(recovery_exc)
                    raise CANTransmissionError(str(recovery_exc)) from recovery_exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._status.requested_on = False
            self._status.voltage_v = 0.0
            self._status.current_a = 0.0
            payload = self._payload()
            for attempt in range(3):
                try:
                    self._mcp.send_extended(CONTROL_CAN_ID, payload)
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(SEND_PERIOD_SECONDS)
            try:
                self._mcp.close()
            finally:
                self._closed = True
                if self._process_lock is not None:
                    self._process_lock.close()
                    self._process_lock = None

