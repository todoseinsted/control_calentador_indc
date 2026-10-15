# Control unificado PFC + calentador + Raspberry PLC

Aplicacion Python para controlar desde un unico Industrial Shields Raspberry
PLC 19R:

- PFC TonHe TH30F10025C7-WT mediante MCP2515 por SPI/CAN.
- Calentador de induccion Daobright de 45 kW mediante Modbus RTU por UART TTL.
- Pagina web unica para consignas, encendido, apagado y diagnostico.

El sistema arranca siempre apagado. No restaura automaticamente una orden de
marcha despues de reiniciar el programa o el PLC.

## Arquitectura

```text
Navegador web
      |
Raspberry PLC 19R
      |-- SPI0.1 --> MCP2515/HW-184 --> CAN --> PFC TonHe
      `-- serial0 --> UART aislada --> Modbus --> Calentador
```

No se necesita un ESP32 ni una comunicacion Wi-Fi entre controladores. La red
se utiliza solamente para acceder a la pagina web del PLC.

## Archivos

- `sistema_pfc_calentador.py`: aplicacion unificada y pagina web.
- `pfc_controller.py`: driver y supervision del PFC por SPI/CAN.
- `control_calentador_plc.py`: controlador reutilizable del calentador y
  aplicacion web independiente para diagnostico.
- `tests/test_combined_controller.py`: pruebas de secuencia y parada segura.

## Secuencia de control

El PFC y el calentador se pueden manejar por separado:

- **Encender solo PFC** aplica la consigna CAN y mantiene el registro de marcha
  del calentador en `OFF` cuando este termina de iniciar.
- **Encender calentador** requiere que el PFC ya este encendido. Configura la
  potencia, comprueba comunicacion y alarmas, y finalmente envia `ON`.
- **Apagar calentador** envia `OFF` pero conserva el PFC encendido.
- **APAGAR TODO** envia primero `OFF` al calentador y despues `STOP` al PFC.

Los dos botones de apagado permanecen habilitados mientras se procesan otras
ordenes. Si una alarma ya ejecuto la parada automatica, volver a pulsarlos es
seguro y no genera un error por estar el equipo apagado.

Al pulsar **Encender sistema**:

1. Se reinicia el MCP2515 y se envia `START` al PFC.
2. Se espera el tiempo de estabilizacion configurado, por defecto 2 segundos.
3. Como el PFC alimenta al calentador, se espera hasta 10 segundos a que este
   responda por Modbus.
4. Se envia `OFF` al calentador y se configura su nivel de potencia.
5. Se comprueba nuevamente la comunicacion y la ausencia de alarmas.
6. Se envia `ON` al calentador.

Al pulsar **APAGAR TODO**, cerrar la aplicacion o detectar una falla:

1. Se cancela el latido y se envia `OFF` al calentador.
2. Se envia `STOP` al PFC.

El calentador renueva `ON` cada 4 segundos. El PFC transmite su consigna cada
500 ms y conserva la recuperacion automatica del MCP2515 ante ruido o bus-off.

Provocan una parada conjunta:

- Alarma informada por el calentador.
- Perdida de comunicacion Modbus durante la marcha.
- Falla CAN que no puede recuperar el MCP2515.
- Error al aplicar una nueva potencia durante la marcha.

El software no reemplaza la parada de emergencia cableada, enclavamientos de
caudal y temperatura, contactores ni protecciones electricas.

## Que significa potencia 0 a 230

`0..230` es la escala digital habilitada al configurar `F-02 = 230`. No son
watts, amperes ni kilowatts. `0` es el extremo inferior de la escala, pero el
apagado seguro se realiza siempre con el registro de marcha en `OFF`, no
solamente escribiendo potencia cero. La interfaz muestra tambien el porcentaje
de esa escala:

```text
porcentaje = nivel / 230 * 100
```

Ejemplos:

| Nivel | Porcentaje de escala |
|---:|---:|
| 0 | 0 % |
| 23 | 10 % |
| 115 | 50 % |
| 230 | 100 % |

No debe suponerse que 115 produce exactamente 22,5 kW. La potencia real depende
de `F-00`, tension de alimentacion, bobina, pieza, acoplamiento, frecuencia,
limite de corriente y protecciones. Antes de utilizar niveles altos se debe
medir y construir una tabla de calibracion para la instalacion real.

El calentador es nominalmente de 45 kW y el PFC utilizado admite como maximo
30 kW. El nivel superior permitido durante la operacion debe determinarse por
ensayo controlado y no debe exceder los limites del PFC ni del resto del
sistema.

## Frecuencia y otros parametros

El mapa Modbus documentado solamente permite escribir dos registros:

| Registro | Escritura |
|---:|---|
| `0x0000` | `0` apagar, `1` encender |
| `0x0005` | Nivel de potencia |

Se pueden leer:

| Registro | Lectura |
|---:|---|
| `0x0000` | Estado de marcha |
| `0x0001` | Corriente |
| `0x0002` | Frecuencia |
| `0x0003` | Temperatura |
| `0x0004` | Alarma |
| `0x0005` | Nivel de potencia |

Por lo tanto, la frecuencia se muestra y supervisa pero no se modifica por
Modbus. Los limites `F-04` y `F-FH` se configuran localmente desde el panel del
calentador. No se escriben registros no documentados.

## Cableado

Realizar todas las conexiones con los equipos apagados.

### PFC

- HW-184/MCP2515 con cristal de 8 MHz.
- SPI en `/dev/spidev0.1`.
- CAN clasico a 125 kbit/s.
- Identificador extendido de control `0x080601A0`.
- Conversor de nivel de 3,3 V a 5 V en SPI.
- CAN-H y CAN-L con terminacion correcta.

La linea `INT` no es necesaria para esta version.

### Calentador

| Calentador | Raspberry PLC 19R |
|---|---|
| TX | RX / GPIO15 |
| RX | TX / GPIO14 |
| GND | Referencia de la interfaz aislada |
| VCC | No conectar |

Puerto Linux: `/dev/serial0`, 9600 baudios, 8 bits, sin paridad, 1 bit de
parada. Direccion Modbus predeterminada: 1 (`F-03 = 1`).

Una UART TTL directa no tiene inmunidad industrial ni aislamiento galvanico.
Para la instalacion definitiva junto a equipos de 30/45 kW se recomienda una
interfaz UART aislada, alimentacion aislada, cable corto y separacion respecto
de conductores de potencia.

## Instalacion prevista en el PLC

```bash
cd /home/nferraro/Documents/pfc_calentador_plc
python3 -m pip install -r requirements.txt
```

El usuario debe pertenecer a los grupos `spi` y `dialout`, y deben existir:

```text
/dev/spidev0.1
/dev/serial0
```

No se deben ejecutar simultaneamente el controlador unificado, el controlador
PFC anterior o la aplicacion independiente del calentador. Los programas usan
los mismos puertos fisicos.

## Primera comprobacion sin encender

El modo siguiente intenta `OFF`, fuerza `STOP`, imprime el estado y termina:

```bash
python3 sistema_pfc_calentador.py --check-only
```

No envia ninguna orden `START` u `ON`. Como el calentador recibe su alimentacion
del PFC, es normal que aparezca sin respuesta en esta comprobacion con el PFC
apagado. Su comunicacion se verifica durante la secuencia normal, despues de
arrancar y estabilizar el PFC.

## Uso normal

```bash
python3 sistema_pfc_calentador.py
```

Abrir desde un equipo de la misma red:

```text
http://IP_DEL_PLC:8080/
```

La pagina permite:

- Introducir tension y corriente del PFC.
- Introducir el nivel `0..230` del calentador.
- Encender solamente el PFC y mantener apagado el calentador.
- Encender o apagar el calentador sin detener el PFC.
- Arrancar el conjunto con la secuencia coordinada.
- Cambiar el nivel de potencia durante la marcha.
- Apagar ambos equipos.
- Consultar estado CAN, recuperaciones, corriente, frecuencia, temperatura y
  alarmas.
- Forzar una recuperacion CAN del PFC.

La pagina no tiene autenticacion. El puerto 8080 debe quedar restringido a una
red local confiable y nunca exponerse directamente a Internet.

## Control por terminal

Todas las operaciones de la pagina utilizan la misma API HTTP:

```bash
# Encender solamente el PFC
curl -X POST -H 'Content-Type: application/json' \
  -d '{"action":"start_pfc","voltage":311,"current":10}' \
  http://IP_DEL_PLC:8080/api/control

# Encender el calentador con el PFC ya activo
curl -X POST -H 'Content-Type: application/json' \
  -d '{"action":"start_heater","heater_power":5}' \
  http://IP_DEL_PLC:8080/api/control

# Apagar solamente el calentador
curl -X POST -H 'Content-Type: application/json' \
  -d '{"action":"stop_heater"}' \
  http://IP_DEL_PLC:8080/api/control

# Apagar calentador y PFC
curl -X POST -H 'Content-Type: application/json' \
  -d '{"action":"stop"}' \
  http://IP_DEL_PLC:8080/api/control
```

## Opciones principales

```text
--serial-device /dev/serial0
--slave 1
--spi-bus 0
--spi-device 1
--spi-speed 500000
--stabilization 2
--listen 0.0.0.0
--port 8080
--log-file ~/Documents/control-pfc-calentador.log
```

El registro rota automaticamente para evitar crecimiento ilimitado.

## Pruebas de software

Las pruebas no abren SPI ni UART y no pueden encender los equipos:

```bash
python3 -m unittest discover -s tests -v
```

Verifican la secuencia de arranque y parada, alarmas, perdida de comunicacion,
falla CAN, escala de potencia y codificacion de la trama PFC.
