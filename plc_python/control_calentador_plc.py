#!/usr/bin/env python3
"""Validacion de solo lectura del calentador Daobright por UART Modbus RTU."""

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
from flask import Flask, Response, jsonify


DEFAULT_DEVICE = "/dev/serial0"
DEFAULT_SLAVE_ADDRESS = 1
DEFAULT_HTTP_PORT = 8080
POLL_INTERVAL_SECONDS = 2.0


@dataclass
class HeaterStatus:
    online: bool = False
    switch: int | None = None
    current_raw: int | None = None
    frequency_raw: int | None = None
    temperature_raw: int | None = None
    alarm: int | None = None
    power_raw: int | None = None
    register_count: int = 0
    updated_at: float = 0.0
    error: str = "Todavia no se consulto el calentador"


class HeaterReader:
    """Acceso Modbus serializado. Esta clase no implementa escrituras."""

    def __init__(self, device: str, slave_address: int) -> None:
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

    def read_status(self) -> HeaterStatus:
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
                    register_count=len(registers),
                    updated_at=time.time(),
                    error="",
                )
            except (minimalmodbus.ModbusException, serial.SerialException, OSError) as exc:
                return HeaterStatus(
                    online=False,
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
  <title>Validacion del calentador</title>
  <style>
    *{box-sizing:border-box} body{font-family:system-ui;background:#101820;color:#eef2f5;
    margin:0;padding:20px}.card{max-width:720px;margin:auto;background:#192631;
    border:1px solid #334655;border-radius:18px;padding:22px;box-shadow:0 12px 35px #0006}
    h1{font-size:1.45rem;margin:0 0 6px}.safe{color:#9cdbbc}.bad{color:#ff9c93}
    .banner,.item{background:#101820;border-radius:12px;padding:14px}.banner{margin-top:16px}
    .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-top:18px}
    .label{color:#9dafbd;font-size:.78rem;text-transform:uppercase}.value{font-size:1.3rem;
    margin-top:4px}.note{color:#9dafbd;font-size:.88rem;line-height:1.45;margin-top:18px}
    @media(max-width:520px){.grid{grid-template-columns:1fr}}
  </style>
</head>
<body><main class="card">
  <h1>Calentador de induccion</h1>
  <div class="safe">Raspberry PLC - validacion Modbus de solo lectura</div>
  <div id="connection" class="banner">Consultando...</div>
  <div class="grid">
    <div class="item"><div class="label">Marcha</div><div id="switch" class="value">-</div></div>
    <div class="item"><div class="label">Alarma</div><div id="alarm" class="value">-</div></div>
    <div class="item"><div class="label">Corriente (crudo)</div><div id="current" class="value">-</div></div>
    <div class="item"><div class="label">Frecuencia (crudo)</div><div id="frequency" class="value">-</div></div>
    <div class="item"><div class="label">Temperatura (crudo)</div><div id="temperature" class="value">-</div></div>
    <div class="item"><div class="label">Potencia (crudo)</div><div id="power" class="value">-</div></div>
  </div>
  <p class="note">UART TTL directa a 9600 8N1, direccion Modbus 1. Esta aplicacion no
  contiene rutas ni funciones de escritura, encendido o cambio de potencia.</p>
</main>
<script>
const value=(id,v)=>document.getElementById(id).textContent=v??'-';
async function update(){
  const banner=document.getElementById('connection');
  try{
    const response=await fetch('/api/status',{cache:'no-store'});
    const data=await response.json();
    if(!data.online){
      banner.className='banner bad'; banner.textContent='Sin comunicacion: '+data.error;
      ['switch','alarm','current','frequency','temperature','power'].forEach(id=>value(id,'-'));
      return;
    }
    banner.className='banner safe'; banner.textContent='Comunicacion correcta - '+data.age_seconds.toFixed(1)+' s desde la ultima lectura';
    value('switch',data.switch===1?'Encendido':'Apagado'); value('alarm',data.alarm);
    value('current',data.current_raw); value('frequency',data.frequency_raw);
    value('temperature',data.temperature_raw); value('power',data.power_raw);
  }catch(error){banner.className='banner bad';banner.textContent='No se pudo consultar la aplicacion web';}
}
update(); setInterval(update,2000);
</script></body></html>
"""


def make_payload(status: HeaterStatus) -> dict[str, Any]:
    payload = asdict(status)
    payload["age_seconds"] = max(0.0, time.time() - status.updated_at) if status.updated_at else 0.0
    return payload


def poll_forever(reader: HeaterReader, store: StatusStore, stop: threading.Event) -> None:
    while not stop.is_set():
        store.set(reader.read_status())
        stop.wait(POLL_INTERVAL_SECONDS)


def build_app(store: StatusStore) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        return Response(PAGE, content_type="text/html; charset=utf-8")

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(make_payload(store.get()))

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

    reader = HeaterReader(args.device, args.slave)
    atexit.register(reader.close)

    if args.once:
        status = reader.read_status()
        print(json.dumps(make_payload(status), ensure_ascii=False, indent=2))
        return 0 if status.online else 1

    store = StatusStore()
    store.set(reader.read_status())
    stop = threading.Event()
    poller = threading.Thread(target=poll_forever, args=(reader, store, stop), daemon=True)
    poller.start()

    app = build_app(store)
    try:
        app.run(host=args.listen, port=args.port, debug=False, threaded=True, use_reloader=False)
    finally:
        stop.set()
        poller.join(timeout=2)
        reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
