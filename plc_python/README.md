# Raspberry PLC 19R - control del calentador

Control Modbus RTU entre un Industrial Shields Raspberry PLC 19R y el
calentador Daobright de 45 kW. Puede manejarse desde la pagina web o importarse
como modulo para usar entradas, salidas y cualquier otra condicion del PLC.

## Cableado UART TTL

Con ambos equipos apagados:

| Calentador | Raspberry PLC 19R |
|---|---|
| TX | RX (GPIO15) |
| RX | TX (GPIO14) |
| GND | GND |
| VCC | Sin conectar |

No conectar RX/TX del calentador a los bornes A+/B- del PLC. Esta prueba usa el
puerto TTL `/dev/serial0`, no los puertos RS-485 `/dev/ttySC*`.

La UART directa no aporta aislamiento galvanico ni la inmunidad al ruido de
RS-485. Es adecuada para validar la comunicacion con cable corto; la instalacion
definitiva junto a un calentador de 45 kW deberia utilizar aislamiento.

## Ajustes

- Calentador: `F-02 = 230` y `F-03 = 1`.
- UART: 9600 baudios, 8 bits, sin paridad y 1 bit de parada.
- Puerto Linux: `/dev/serial0`.

## Ejecucion

Las dependencias ya estaban instaladas en el PLC inspeccionado. Para una sola
lectura:

```bash
cd /home/RaspPLC1/Ensayos
python3 control_calentador_plc.py --once
```

Para levantar la pagina web:

```bash
cd /home/RaspPLC1/Ensayos
python3 control_calentador_plc.py
```

Abrir `http://192.168.0.157:8080`. La pagina permite encender, apagar y ajustar
la potencia entre 0 y 230. El estado se actualiza sin recargar la pagina.
`/api/status` devuelve JSON y `/health` responde 200 cuando la lectura Modbus es
correcta.

## Uso desde la logica del PLC

La clase `HeaterController` es independiente de Flask. Un programa futuro puede
importarla y usar siempre los mismos metodos:

```python
from control_calentador_plc import HeaterController

heater = HeaterController()
heater.set_power(100)

if condicion_de_entradas:
    heater.turn_on()
else:
    heater.turn_off()
```

Mientras exista una orden de marcha hay que llamar periodicamente a
`heater.keep_alive_if_due()`. Debe hacerse como minimo una vez cada cuatro
segundos, porque el calentador exige renovar la orden antes de cinco segundos.
La aplicacion web incluida ya mantiene esta renovacion en un hilo propio.

Las llamadas estan protegidas para poder compartir una unica instancia entre
la web y la logica de entradas/salidas. Ante una falla durante la renovacion se
anula la orden interna de marcha, por lo que el equipo no vuelve a arrancar solo
cuando regresa la comunicacion. Al cerrar normalmente la aplicacion tambien se
envia una orden de apagado si estaba en marcha.
