#!/usr/bin/env python3
"""Control coordinado del PFC TonHe y calentador Daobright desde el PLC."""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from logging.handlers import RotatingFileHandler
from typing import Any

import minimalmodbus
import serial
from flask import Flask, Response, jsonify, request

from control_calentador_plc import HeaterController, HeaterStatus, MAX_POWER, MIN_POWER
from pfc_controller import PFCController, PFCError, PFCStatus, validate_start_values


DEFAULT_HTTP_PORT = 8080
DEFAULT_STABILIZATION_SECONDS = 2.0
HEATER_READY_TIMEOUT_SECONDS = 10.0
HEATER_POLL_SECONDS = 1.0
SUPERVISOR_PERIOD_SECONDS = 0.1

ALARM_TEXT = {
    0x00: "Normal",
    0x01: "Sensor de corriente o bobina (E1)",
    0x02: "Tension baja (E2)",
    0x03: "Sensor de temperatura de bobina (E3)",
    0x04: "Temperatura IGBT alta (E4)",
    0x05: "Temperatura ambiente baja (E5)",
    0x06: "Falla IGBT (E6)",
    0x08: "Sensor Hall (E8)",
    0x09: "Sin carga (E9)",
    0x0A: "Sensor de temperatura de bobina (EA)",
    0x0C: "Falla de bobina (EC)",
}


class SystemControlError(RuntimeError):
    """No se pudo ejecutar una secuencia coordinada de control."""


@dataclass
class SystemStatus:
    mode: str = "off"
    message: str = "Arranque seguro: sistema apagado"
    message_is_error: bool = False
    pfc_voltage_v: float = 0.0
    pfc_current_a: float = 0.0
    heater_power_level: int = 0
    transition_remaining_s: float = 0.0
    updated_at: float = 0.0


def validate_heater_power(power: int) -> None:
    if isinstance(power, bool) or not isinstance(power, int):
        raise ValueError("La potencia del calentador debe ser un numero entero")
    if not MIN_POWER <= power <= MAX_POWER:
        raise ValueError(
            f"El nivel de potencia debe estar entre {MIN_POWER} y {MAX_POWER}"
        )


def power_level_percent(power: int | None) -> float | None:
    if power is None:
        return None
    return round(power * 100.0 / MAX_POWER, 1)


class CombinedController:
    """Orquesta PFC y calentador con parada conjunta ante fallas."""

    def __init__(
        self,
        pfc: PFCController,
        heater: HeaterController,
        stabilization_seconds: float = DEFAULT_STABILIZATION_SECONDS,
    ) -> None:
        if stabilization_seconds < 0:
            raise ValueError("El tiempo de estabilizacion no puede ser negativo")
        self.pfc = pfc
        self.heater = heater
        self.stabilization_seconds = stabilization_seconds
        self._lock = threading.RLock()
        self._commands = threading.Lock()
        self._state = SystemStatus(updated_at=time.time())
        self._heater_status = HeaterStatus()
        self._last_heater_poll = 0.0
        self._start_deadline = 0.0
        self._heater_ready_deadline = 0.0
        self._closed = False

    def _set_message(self, message: str, is_error: bool = False) -> None:
        changed = (
            message != self._state.message
            or is_error != self._state.message_is_error
        )
        self._state.message = message
        self._state.message_is_error = is_error
        self._state.updated_at = time.time()
        if changed:
            (logging.error if is_error else logging.info)(message)

    @staticmethod
    def _alarm_description(code: int | None) -> str:
        if code is None:
            return "sin codigo"
        return ALARM_TEXT.get(code, f"codigo 0x{code:02X}")

    def safe_startup(self) -> None:
        """Intenta apagar la carga y fuerza STOP en el PFC."""
        with self._commands, self._lock:
            heater_warning = ""
            pfc_error = ""
            try:
                self.heater.turn_off()
            except Exception as exc:
                # El PFC alimenta al calentador. Si el PFC ya estaba apagado,
                # no puede existir respuesta Modbus y esto es esperable.
                heater_warning = str(exc)
            try:
                self.pfc.stop()
            except Exception as exc:
                pfc_error = str(exc)
            self._state.mode = "fault" if pfc_error else "off"
            self._state.pfc_voltage_v = 0.0
            self._state.pfc_current_a = 0.0
            if pfc_error:
                self._set_message(
                    "No se pudo confirmar STOP del PFC: " + pfc_error,
                    True,
                )
            elif heater_warning:
                self._set_message(
                    "PFC detenido; calentador sin alimentacion o sin respuesta "
                    "(estado esperado con el PFC apagado)"
                )
            else:
                self._set_message("Arranque seguro confirmado: sistema apagado")

    def start_system(self, voltage: float, current: float, heater_power: int) -> None:
        """Arranca el PFC y habilita el calentador cuando ya tiene alimentacion."""
        validate_start_values(voltage, current)
        validate_heater_power(heater_power)
        with self._commands, self._lock:
            if self._state.mode in {"starting", "running", "stopping"}:
                raise SystemControlError("Primero detenga el sistema")
            self._state.mode = "starting"
            self._set_message("Arrancando PFC para alimentar el calentador")
            try:
                self.pfc.start(voltage, current)
            except Exception as exc:
                self._safe_stop_locked(f"Fallo durante el arranque: {exc}", fault=True)
                raise SystemControlError(str(exc)) from exc

            self._state.pfc_voltage_v = float(voltage)
            self._state.pfc_current_a = float(current)
            self._state.heater_power_level = heater_power
            self._start_deadline = time.monotonic() + self.stabilization_seconds
            self._heater_ready_deadline = (
                self._start_deadline + HEATER_READY_TIMEOUT_SECONDS
            )
            self._set_message(
                f"PFC solicitado; esperando {self.stabilization_seconds:g} s antes del calentador"
            )

    def _safe_stop_locked(self, reason: str, fault: bool) -> None:
        self._state.mode = "stopping"
        errors: list[str] = []
        try:
            self.heater.turn_off()
        except Exception as exc:
            errors.append(f"OFF calentador: {exc}")
        try:
            self.pfc.stop()
        except Exception as exc:
            errors.append(f"STOP PFC: {exc}")

        self._state.pfc_voltage_v = 0.0
        self._state.pfc_current_a = 0.0
        self._start_deadline = 0.0
        self._heater_ready_deadline = 0.0
        self._state.mode = "fault" if fault or errors else "off"
        message = reason
        if errors:
            message += "; " + "; ".join(errors)
        self._set_message(message, fault or bool(errors))

    def stop_system(self, reason: str = "Parada solicitada por el usuario") -> None:
        with self._commands, self._lock:
            self._safe_stop_locked(reason, fault=False)

    def emergency_stop(self, reason: str) -> None:
        with self._commands, self._lock:
            self._safe_stop_locked(f"Parada de seguridad: {reason}", fault=True)

    def set_heater_power(self, power: int) -> None:
        validate_heater_power(power)
        with self._commands, self._lock:
            try:
                self.heater.set_power(power)
                self._state.heater_power_level = power
                self._set_message(
                    f"Potencia configurada en {power}/230 "
                    f"({power_level_percent(power):.1f}% de escala)"
                )
            except Exception as exc:
                if self._state.mode in {"starting", "running"}:
                    self._safe_stop_locked(
                        f"Fallo al cambiar potencia: {exc}", fault=True
                    )
                raise

    def recover_pfc(self) -> None:
        with self._commands, self._lock:
            try:
                self.pfc.recover()
                self._set_message("Comunicacion CAN del PFC reinicializada")
            except Exception as exc:
                if self._state.mode in {"starting", "running"}:
                    self._safe_stop_locked(
                        f"No se pudo recuperar el PFC: {exc}", fault=True
                    )
                raise

    def service(self) -> None:
        """Atiende heartbeats, lecturas y transiciones; llamar varias veces/s."""
        with self._lock:
            if self._closed:
                return
            try:
                self.pfc.service()
                self.heater.keep_alive_if_due()

                now = time.monotonic()
                if now - self._last_heater_poll >= HEATER_POLL_SECONDS:
                    self._heater_status = self.heater.read_status()
                    self._last_heater_poll = now

                if self._state.mode == "running":
                    if not self._heater_status.online:
                        raise SystemControlError(
                            "se perdio la comunicacion con el calentador: "
                            + self._heater_status.error
                        )
                    if self._heater_status.alarm not in {None, 0}:
                        raise SystemControlError(
                            "alarma del calentador: "
                            + self._alarm_description(self._heater_status.alarm)
                        )

                if self._state.mode == "starting" and now >= self._start_deadline:
                    pfc_status = self.pfc.status()
                    if not pfc_status.requested_on or not pfc_status.communication_ok:
                        raise SystemControlError(
                            "el PFC no tiene una transmision CAN valida"
                        )
                    if not self._heater_status.online:
                        if now >= self._heater_ready_deadline:
                            raise SystemControlError(
                                "el calentador no respondio despues de recibir "
                                "alimentacion: " + self._heater_status.error
                            )
                        self._set_message(
                            "PFC encendido; esperando que el calentador inicie"
                        )
                        return

                    # Ahora que el calentador tiene alimentacion, confirmar OFF,
                    # aplicar la consigna y comprobar alarmas antes de habilitarlo.
                    self.heater.turn_off()
                    self.heater.set_power(self._state.heater_power_level)
                    self._heater_status = self.heater.read_status()
                    if not self._heater_status.online:
                        raise SystemControlError(
                            "el calentador dejo de responder durante el arranque: "
                            + self._heater_status.error
                        )
                    if self._heater_status.alarm not in {None, 0}:
                        raise SystemControlError(
                            "el calentador informa "
                            + self._alarm_description(self._heater_status.alarm)
                        )
                    self.heater.turn_on()
                    self._state.mode = "running"
                    self._set_message("PFC y calentador encendidos")
            except Exception as exc:
                if self._state.mode in {"starting", "running"}:
                    self._safe_stop_locked(str(exc), fault=True)
                else:
                    self._set_message(f"Fallo de supervision: {exc}", True)

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            state = asdict(self._state)
            if self._state.mode == "starting":
                now = time.monotonic()
                deadline = (
                    self._start_deadline
                    if now < self._start_deadline
                    else self._heater_ready_deadline
                )
                state["transition_remaining_s"] = round(
                    max(0.0, deadline - now), 1
                )
            else:
                state["transition_remaining_s"] = 0.0
            heater = asdict(self._heater_status)
            heater["power_percent"] = power_level_percent(
                self._heater_status.power_raw
            )
            heater["requested_power_level"] = self._state.heater_power_level
            heater["requested_power_percent"] = power_level_percent(
                self._state.heater_power_level
            )
            heater["alarm_text"] = self._alarm_description(
                self._heater_status.alarm
            )
            return {
                "system": state,
                "pfc": self.pfc.status().to_dict(),
                "heater": heater,
            }

    def close(self) -> None:
        with self._commands, self._lock:
            if self._closed:
                return
            try:
                self._safe_stop_locked("Aplicacion cerrada: sistema apagado", False)
            finally:
                self.heater.close()
                self.pfc.close()
                self._closed = True


PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PFC + calentador</title><style>
*{box-sizing:border-box}body{font-family:system-ui;margin:0;padding:20px;background:#0d151b;color:#edf4f7}
.wrap{max-width:980px;margin:auto}.card{background:#17252f;border:1px solid #304754;border-radius:16px;padding:20px;margin-bottom:15px}
h1{margin:0 0 5px;font-size:1.55rem}h2{font-size:1.05rem;margin:0 0 13px}.muted{color:#9eb0ba}.ok{color:#8ce0b6}.bad{color:#ff9e96}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.item{background:#0d151b;border-radius:10px;padding:12px}.label{color:#9eb0ba;font-size:.75rem;text-transform:uppercase}.value{font-size:1.15rem;margin-top:4px}
.fields{display:flex;gap:12px;flex-wrap:wrap;align-items:end}label{font-size:.82rem;color:#b5c5cd}input{display:block;width:145px;margin-top:5px;padding:10px;border:1px solid #4a626f;border-radius:8px;background:#0d151b;color:white;font-size:1rem}
button{padding:11px 17px;border:0;border-radius:9px;font-weight:700;cursor:pointer}.start{background:#35c781;color:#071b12}.stop{background:#ef6259;color:#250604}.secondary{background:#d3a640;color:#211804}button:disabled{opacity:.5}
#message{padding:12px;border-radius:10px;background:#0d151b;margin-top:14px}.note{font-size:.85rem;color:#9eb0ba;line-height:1.45}
@media(max-width:700px){.grid{grid-template-columns:1fr 1fr}}@media(max-width:480px){.grid{grid-template-columns:1fr}.fields>*{width:100%}input{width:100%}}
</style></head><body><main class="wrap">
<div class="card"><h1>Sistema PFC + calentador</h1><div class="muted">Raspberry PLC 19R</div><div id="message">Consultando...</div></div>
<div class="card"><h2>Arranque coordinado</h2><div class="fields">
<label>Tension PFC (V)<input id="voltage" type="number" min="150" max="1000" step="0.1" value="311"></label>
<label>Corriente PFC (A)<input id="currentSet" type="number" min="2" max="120" step="0.01" value="10"></label>
<label>Potencia calentador (0-230)<input id="heaterPowerSet" type="number" min="0" max="230" step="1" value="10"></label>
<button class="start" onclick="startSystem()">Encender sistema</button><button class="stop" onclick="command({action:'stop'})">APAGAR TODO</button>
</div><p class="note">0-230 es una escala de mando, no son watts. Nivel 0 no reemplaza la orden OFF. El calentador se habilita despues de estabilizar el PFC.</p></div>
<div class="card"><h2>PFC</h2><div class="grid">
<div class="item"><div class="label">Orden</div><div id="pfcOrder" class="value">-</div></div>
<div class="item"><div class="label">Consigna</div><div id="pfcSet" class="value">-</div></div>
<div class="item"><div class="label">CAN TX</div><div id="pfcCan" class="value">-</div></div>
<div class="item"><div class="label">Tramas</div><div id="pfcFrames" class="value">-</div></div>
<div class="item"><div class="label">Recuperaciones</div><div id="pfcRecoveries" class="value">-</div></div>
<div class="item"><button class="secondary" onclick="command({action:'recover_pfc'})">Recuperar CAN</button></div>
</div></div>
<div class="card"><h2>Calentador</h2><div class="grid">
<div class="item"><div class="label">Comunicacion</div><div id="heaterOnline" class="value">-</div></div>
<div class="item"><div class="label">Marcha informada</div><div id="heaterSwitch" class="value">-</div></div>
<div class="item"><div class="label">Alarma</div><div id="heaterAlarm" class="value">-</div></div>
<div class="item"><div class="label">Potencia</div><div id="heaterPower" class="value">-</div></div>
<div class="item"><div class="label">Corriente Modbus</div><div id="heaterCurrent" class="value">-</div></div>
<div class="item"><div class="label">Frecuencia Modbus</div><div id="heaterFrequency" class="value">-</div></div>
<div class="item"><div class="label">Temperatura Modbus</div><div id="heaterTemperature" class="value">-</div></div>
</div><div class="fields" style="margin-top:12px"><label>Nuevo nivel<input id="newPower" type="number" min="0" max="230" step="1" value="10"></label><button class="secondary" onclick="setPower()">Cambiar potencia</button></div></div>
</main><script>
const el=id=>document.getElementById(id); const show=(id,v)=>el(id).textContent=(v??'-');
async function command(body){document.querySelectorAll('button').forEach(b=>b.disabled=true);try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw new Error(d.error||'Error');await update();}catch(e){el('message').className='bad';el('message').textContent=e.message;}finally{document.querySelectorAll('button').forEach(b=>b.disabled=false)}}
function startSystem(){command({action:'start',voltage:Number(el('voltage').value),current:Number(el('currentSet').value),heater_power:Number(el('heaterPowerSet').value)})}
function setPower(){command({action:'power',heater_power:Number(el('newPower').value)})}
async function update(){try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();const s=d.system,p=d.pfc,h=d.heater;el('message').className=s.message_is_error?'bad':'ok';el('message').textContent=s.mode.toUpperCase()+': '+s.message+(s.transition_remaining_s?' ('+s.transition_remaining_s+' s)':'');show('pfcOrder',p.requested_on?'START':'STOP');show('pfcSet',p.voltage_v.toFixed(1)+' V / '+p.current_a.toFixed(2)+' A');show('pfcCan',p.communication_ok?'Correcto':'Falla');show('pfcFrames',p.frames_sent);show('pfcRecoveries',p.recoveries);show('heaterOnline',h.online?'En linea':'Sin respuesta');show('heaterSwitch',h.switch===1?'Encendido':'Apagado');show('heaterAlarm',h.alarm_text);show('heaterPower',h.power_raw==null?'-':h.power_raw+'/230 ('+h.power_percent+'%)');show('heaterCurrent',h.current_raw);show('heaterFrequency',h.frequency_raw);show('heaterTemperature',h.temperature_raw);}catch(e){el('message').className='bad';el('message').textContent='No se pudo consultar el PLC';}}
update();setInterval(update,1000);
</script></body></html>"""


def supervise_forever(
    controller: CombinedController, stop_event: threading.Event
) -> None:
    while not stop_event.is_set():
        controller.service()
        stop_event.wait(SUPERVISOR_PERIOD_SECONDS)


def build_app(controller: CombinedController) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        response = Response(PAGE, content_type="text/html; charset=utf-8")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(controller.status_payload())

    @app.get("/health")
    def health() -> tuple[Response, int]:
        payload = controller.status_payload()
        healthy = (
            payload["pfc"]["communication_ok"]
            and payload["heater"]["online"]
            and payload["system"]["mode"] != "fault"
        )
        return jsonify({"ok": healthy, **payload}), 200 if healthy else 503

    @app.post("/api/control")
    def api_control() -> tuple[Response, int] | Response:
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        try:
            if action == "start":
                voltage = float(data.get("voltage"))
                current = float(data.get("current"))
                heater_power = data.get("heater_power")
                if isinstance(heater_power, float) and heater_power.is_integer():
                    heater_power = int(heater_power)
                controller.start_system(voltage, current, heater_power)
                message = "Secuencia de arranque iniciada"
            elif action == "stop":
                controller.stop_system()
                message = "Sistema apagado"
            elif action == "power":
                heater_power = data.get("heater_power")
                if isinstance(heater_power, float) and heater_power.is_integer():
                    heater_power = int(heater_power)
                controller.set_heater_power(heater_power)
                message = "Potencia actualizada"
            elif action == "recover_pfc":
                controller.recover_pfc()
                message = "CAN reinicializado"
            else:
                return jsonify({"ok": False, "error": "Accion no valida"}), 400
            return jsonify({"ok": True, "message": message})
        except (TypeError, ValueError, SystemControlError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (PFCError, minimalmodbus.ModbusException, serial.SerialException, OSError) as exc:
            logging.exception("Fallo el comando %s", action)
            return jsonify({"ok": False, "error": f"Fallo de comunicacion: {exc}"}), 503

    return app


def configure_logging(log_file: str) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    expanded = os.path.expanduser(log_file)
    handler = RotatingFileHandler(
        expanded, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-device", default="/dev/serial0")
    parser.add_argument("--slave", type=int, default=1)
    parser.add_argument("--spi-bus", type=int, default=0)
    parser.add_argument("--spi-device", type=int, default=1)
    parser.add_argument("--spi-speed", type=int, default=500_000)
    parser.add_argument("--stabilization", type=float, default=DEFAULT_STABILIZATION_SECONDS)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="intenta OFF, fuerza STOP, imprime el estado y termina",
    )
    parser.add_argument(
        "--log-file", default="~/Documents/control-pfc-calentador.log"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_file)
    heater: HeaterController | None = None
    pfc: PFCController | None = None
    combined: CombinedController | None = None
    stop_event = threading.Event()
    supervisor: threading.Thread | None = None
    try:
        heater = HeaterController(args.serial_device, args.slave)
        pfc = PFCController(args.spi_bus, args.spi_device, args.spi_speed)
        combined = CombinedController(pfc, heater, args.stabilization)
        combined.safe_startup()
        if args.check_only:
            combined.service()
            print(
                json.dumps(
                    combined.status_payload(), ensure_ascii=False, indent=2
                )
            )
            status = combined.status_payload()
            heater_alarm = status["heater"]["alarm"]
            return 0 if (
                status["pfc"]["communication_ok"]
                and heater_alarm in {None, 0}
            ) else 1
        supervisor = threading.Thread(
            target=supervise_forever,
            args=(combined, stop_event),
            daemon=True,
            name="supervisor-pfc-calentador",
        )
        supervisor.start()
        app = build_app(combined)
        app.run(
            host=args.listen,
            port=args.port,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
        return 0
    except Exception:
        logging.exception("No se pudo iniciar el controlador combinado")
        return 1
    finally:
        stop_event.set()
        if supervisor is not None:
            supervisor.join(timeout=2)
        if combined is not None:
            combined.close()
        else:
            if heater is not None:
                heater.close()
            if pfc is not None:
                pfc.close()


if __name__ == "__main__":
    raise SystemExit(main())

