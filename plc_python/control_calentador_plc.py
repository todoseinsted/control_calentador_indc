#!/usr/bin/env python3
"""Control del calentador Daobright por UART Modbus RTU y pagina web."""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

import minimalmodbus
import serial
from flask import Flask, Response, jsonify, request


DEFAULT_DEVICE = "/dev/serial0"
DEFAULT_SLAVE_ADDRESS = 1
DEFAULT_HTTP_PORT = 8080
POLL_INTERVAL_SECONDS = 1.0
KEEPALIVE_INTERVAL_SECONDS = 4.0
MIN_POWER = 0
MAX_POWER = 230


@dataclass
class HeaterStatus:
    online: bool = False
    switch: int | None = None
    current_raw: int | None = None
    frequency_raw: int | None = None
    temperature_raw: int | None = None
    alarm: int | None = None
    power_raw: int | None = None
    requested_on: bool = False
    register_count: int = 0
    updated_at: float = 0.0
    error: str = "Todavia no se consulto el calentador"


class HeaterController:
    """Interfaz reutilizable para la web y para la logica propia del PLC.

    Los metodos publicos son seguros para usar desde distintos hilos. Cuando
    turn_on() activa el equipo, keep_alive_if_due() debe ejecutarse
    periodicamente para renovar la orden antes de los cinco segundos exigidos
    por el calentador. La aplicacion incluida ya lo hace automaticamente.
    """

    def __init__(self, device: str = DEFAULT_DEVICE, slave_address: int = DEFAULT_SLAVE_ADDRESS) -> None:
        self._instrument = minimalmodbus.Instrument(
            device,
            slave_address,
            mode=minimalmodbus.MODE_RTU,
            close_port_after_each_call=False,
            debug=False,
        )
        self._instrument.serial.baudrate = 9600
        self._instrument.serial.bytesize = 8
        self._instrument.serial.parity = serial.PARITY_NONE
        self._instrument.serial.stopbits = 1
        self._instrument.serial.timeout = 0.5
        self._instrument.clear_buffers_before_each_transaction = True
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._requested_on = False
        self._last_keepalive = 0.0

    @property
    def requested_on(self) -> bool:
        with self._state_lock:
            return self._requested_on

    def _write_register(self, address: int, value: int) -> None:
        with self._io_lock:
            self._instrument.write_register(
                registeraddress=address,
                value=value,
                number_of_decimals=0,
                functioncode=6,
                signed=False,
            )

    def turn_on(self) -> None:
        """Enciende el calentador y habilita la renovacion automatica."""
        with self._command_lock:
            self._write_register(0, 1)
            with self._state_lock:
                self._requested_on = True
                self._last_keepalive = time.monotonic()

    def turn_off(self) -> None:
        """Deshabilita primero la renovacion y luego ordena el apagado."""
        with self._command_lock:
            with self._state_lock:
                self._requested_on = False
            self._write_register(0, 0)

    def set_power(self, power: int) -> None:
        """Fija la potencia de comunicacion admitida por F-02 (0 a 230)."""
        if isinstance(power, bool) or not isinstance(power, int):
            raise ValueError("La potencia debe ser un numero entero")
        if not MIN_POWER <= power <= MAX_POWER:
            raise ValueError(f"La potencia debe estar entre {MIN_POWER} y {MAX_POWER}")
        self._write_register(5, power)

    def keep_alive_if_due(self) -> bool:
        """Renueva la marcha cuando corresponde. Devuelve True si escribio."""
        with self._command_lock:
            with self._state_lock:
                due = self._requested_on and (
                    time.monotonic() - self._last_keepalive >= KEEPALIVE_INTERVAL_SECONDS
                )
            if not due:
                return False

            try:
                self._write_register(0, 1)
            except Exception:
                # No reanudar automaticamente luego de perder comunicacion.
                with self._state_lock:
                    self._requested_on = False
                raise

            with self._state_lock:
                self._last_keepalive = time.monotonic()
            return True

    def read_status(self) -> HeaterStatus:
        requested_on = self.requested_on
        with self._io_lock:
            try:
                try:
                    registers = self._instrument.read_registers(
                        registeraddress=0,
                        number_of_registers=6,
                        functioncode=3,
                    )
                except minimalmodbus.ModbusException:
                    # El manual enumera seis registros, pero un ejemplo solicita cinco.
                    registers = self._instrument.read_registers(
                        registeraddress=0,
                        number_of_registers=5,
                        functioncode=3,
                    )

                return HeaterStatus(
                    online=True,
                    switch=registers[0],
                    current_raw=registers[1],
                    frequency_raw=registers[2],
                    temperature_raw=registers[3],
                    alarm=registers[4],
                    power_raw=registers[5] if len(registers) >= 6 else None,
                    requested_on=requested_on,
                    register_count=len(registers),
                    updated_at=time.time(),
                    error="",
                )
            except (minimalmodbus.ModbusException, serial.SerialException, OSError) as exc:
                return HeaterStatus(
                    online=False,
                    requested_on=requested_on,
                    updated_at=time.time(),
                    error=f"{type(exc).__name__}: {exc}",
                )

    def close(self) -> None:
        with self._io_lock:
            if self._instrument.serial.is_open:
                self._instrument.serial.close()


class StatusStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = HeaterStatus()

    def set(self, status: HeaterStatus) -> None:
        with self._lock:
            self._status = status

    def get(self) -> HeaterStatus:
        with self._lock:
            return HeaterStatus(**asdict(self._status))


PAGE = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Control del calentador</title>
  <style>
    *{box-sizing:border-box} body{font-family:system-ui;background:#101820;color:#eef2f5;
    margin:0;padding:20px}.card{max-width:720px;margin:auto;background:#192631;
    border:1px solid #334655;border-radius:18px;padding:22px;box-shadow:0 12px 35px #0006}
    h1{font-size:1.45rem;margin:0 0 6px}.safe{color:#9cdbbc}.bad{color:#ff9c93}
    .banner,.item,.controls{background:#101820;border-radius:12px;padding:14px}.banner{margin-top:16px}
    .controls{margin-top:14px}.buttons{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
    button{border:0;border-radius:10px;padding:12px 20px;font-weight:700;cursor:pointer}
    button:disabled{opacity:.45;cursor:wait}.on{background:#33c481;color:#071d14}.off{background:#f06b61;color:#240604}
    .set{background:#d9e2e8;color:#10202b}input{width:110px;border:1px solid #526879;
    border-radius:9px;padding:10px;background:#192631;color:#fff;font-size:1rem}
    .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-top:14px}
    .label{color:#9dafbd;font-size:.78rem;text-transform:uppercase}.value{font-size:1.3rem;margin-top:4px}
    .note{color:#9dafbd;font-size:.88rem;line-height:1.45;margin-top:18px}
    #message{min-height:1.4em;margin-top:10px;color:#f5d98a}
    @media(max-width:520px){.grid{grid-template-columns:1fr}}
  </style>
</head>
<body><main class="card">
  <h1>Calentador de induccion</h1>
  <div class="safe">Raspberry PLC - control Modbus</div>
  <div id="connection" class="banner">Consultando...</div>
  <section class="controls">
    <div class="label">Mando</div>
    <div class="buttons">
      <button class="on" onclick="command('on')">Encender</button>
      <button class="off" onclick="command('off')">Apagar</button>
    </div>
    <div class="buttons">
      <label>Potencia <input id="powerInput" type="number" min="0" max="230" step="1" value="100"></label>
      <button class="set" onclick="setPower()">Aplicar potencia</button>
    </div>
    <div id="message"></div>
  </section>
  <div class="grid">
    <div class="item"><div class="label">Marcha real</div><div id="switch" class="value">-</div></div>
    <div class="item"><div class="label">Orden del PLC</div><div id="requested" class="value">-</div></div>
    <div class="item"><div class="label">Alarma</div><div id="alarm" class="value">-</div></div>
    <div class="item"><div class="label">Corriente (crudo)</div><div id="current" class="value">-</div></div>
    <div class="item"><div class="label">Frecuencia (crudo)</div><div id="frequency" class="value">-</div></div>
    <div class="item"><div class="label">Temperatura (crudo)</div><div id="temperature" class="value">-</div></div>
    <div class="item"><div class="label">Potencia (crudo)</div><div id="power" class="value">-</div></div>
  </div>
  <p class="note">La pagina y la logica interna del PLC comparten el mismo controlador.
  Si falla una renovacion de marcha, la orden queda desactivada para evitar un rearranque automatico.</p>
</main>
<script>
const value=(id,v)=>document.getElementById(id).textContent=v??'-';
const message=t=>document.getElementById('message').textContent=t;
async function post(body){
  document.querySelectorAll('button').forEach(b=>b.disabled=true); message('Enviando...');
  try{
    const response=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json();
    if(!response.ok) throw new Error(data.error||'Error de control');
    message(data.message); await update();
  }catch(error){message(error.message);}
  finally{document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
function command(action){post({action});}
function setPower(){post({action:'power',power:Number(document.getElementById('powerInput').value)});}
async function update(){
  const banner=document.getElementById('connection');
  try{
    const response=await fetch('/api/status',{cache:'no-store'}); const data=await response.json();
    if(!data.online){
      banner.className='banner bad'; banner.textContent='Sin comunicacion: '+data.error;
      ['switch','requested','alarm','current','frequency','temperature','power'].forEach(id=>value(id,'-')); return;
    }
    banner.className='banner safe'; banner.textContent='Comunicacion correcta - '+data.age_seconds.toFixed(1)+' s desde la ultima lectura';
    value('switch',data.switch===1?'Encendido':'Apagado'); value('requested',data.requested_on?'Marcha':'Parada');
    value('alarm',data.alarm); value('current',data.current_raw); value('frequency',data.frequency_raw);
    value('temperature',data.temperature_raw); value('power',data.power_raw);
  }catch(error){banner.className='banner bad';banner.textContent='No se pudo consultar la aplicacion web';}
}
update(); setInterval(update,1000);
</script></body></html>
"""


def make_payload(status: HeaterStatus) -> dict[str, Any]:
    payload = asdict(status)
    payload["age_seconds"] = max(0.0, time.time() - status.updated_at) if status.updated_at else 0.0
    return payload


def supervise_forever(controller: HeaterController, store: StatusStore, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            controller.keep_alive_if_due()
            store.set(controller.read_status())
        except (minimalmodbus.ModbusException, serial.SerialException, OSError) as exc:
            store.set(
                HeaterStatus(
                    online=False,
                    requested_on=controller.requested_on,
                    updated_at=time.time(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        stop.wait(POLL_INTERVAL_SECONDS)


def build_app(controller: HeaterController, store: StatusStore) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        response = Response(PAGE, content_type="text/html; charset=utf-8")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(make_payload(store.get()))

    @app.post("/api/control")
    def api_control() -> tuple[Response, int] | Response:
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        try:
            if action == "on":
                controller.turn_on()
                message = "Orden de encendido enviada"
            elif action == "off":
                controller.turn_off()
                message = "Orden de apagado enviada"
            elif action == "power":
                power = data.get("power")
                if isinstance(power, float) and power.is_integer():
                    power = int(power)
                controller.set_power(power)
                message = f"Potencia configurada en {power}"
            else:
                return jsonify({"ok": False, "error": "Accion no valida"}), 400
            store.set(controller.read_status())
            return jsonify({"ok": True, "message": message})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (minimalmodbus.ModbusException, serial.SerialException, OSError) as exc:
            logging.exception("Fallo el comando %s", action)
            return jsonify({"ok": False, "error": f"No se pudo comunicar: {exc}"}), 503

    @app.get("/health")
    def health() -> tuple[Response, int]:
        status = store.get()
        return jsonify({"ok": status.online}), 200 if status.online else 503

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--slave", type=int, default=DEFAULT_SLAVE_ADDRESS)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--once", action="store_true", help="Lee una vez, imprime JSON y termina")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    controller = HeaterController(args.device, args.slave)
    atexit.register(controller.close)

    if args.once:
        status = controller.read_status()
        print(json.dumps(make_payload(status), ensure_ascii=False, indent=2))
        return 0 if status.online else 1

    store = StatusStore()
    store.set(controller.read_status())
    stop = threading.Event()
    supervisor = threading.Thread(
        target=supervise_forever,
        args=(controller, store, stop),
        daemon=True,
    )
    supervisor.start()

    app = build_app(controller, store)
    try:
        app.run(host=args.listen, port=args.port, debug=False, threaded=True, use_reloader=False)
    finally:
        stop.set()
        supervisor.join(timeout=2)
        if controller.requested_on:
            try:
                controller.turn_off()
            except Exception:
                logging.exception("No se pudo enviar el apagado al cerrar")
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
