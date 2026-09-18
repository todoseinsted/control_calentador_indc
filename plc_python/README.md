# Raspberry PLC 19R - validacion del calentador

Prueba de solo lectura para comprobar la comunicacion Modbus RTU entre un
Industrial Shields Raspberry PLC 19R y el calentador Daobright de 45 kW.

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

Abrir `http://192.168.0.157:8080`. El estado se actualiza cada dos segundos sin
recargar la pagina. `/api/status` devuelve JSON y `/health` responde 200 cuando
la lectura Modbus es correcta.

## Seguridad de esta version

El programa no implementa escrituras Modbus. No puede encender, apagar ni
cambiar la potencia del calentador. Su unico objetivo es validar RX, TX, GND,
la direccion Modbus y la lectura de los seis registros documentados.
