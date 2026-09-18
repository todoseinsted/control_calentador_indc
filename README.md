# Control del calentador con Raspberry PLC 19R

Aplicación Python para controlar un calentador de inducción Daobright de 45 kW
desde un Industrial Shields Raspberry PLC 19R mediante Modbus RTU sobre UART
TTL. Incluye una página web y una clase reutilizable para incorporar entradas,
salidas, temporizadores y otras condiciones del PLC sin depender de la web.

## Funciones disponibles

- Lectura de marcha, corriente, frecuencia, temperatura, alarma y potencia.
- Encendido y apagado desde la página web.
- Ajuste de potencia entre `0` y `230`.
- Renovación automática de la orden de marcha cada cuatro segundos.
- API HTTP para integrar otro sistema de supervisión.
- Clase `HeaterController` para controlar el equipo directamente desde Python.
- Acceso serial protegido para compartir el controlador entre la web y la
  lógica del PLC.
- Apagado seguro al cerrar normalmente la aplicación.
- Bloqueo del rearranque automático si falla una renovación de marcha.

## Cableado UART TTL

Realizar el cableado con ambos equipos apagados:

| Calentador | Raspberry PLC 19R |
|---|---|
| TX | RX (GPIO15) |
| RX | TX (GPIO14) |
| GND | GND |
| VCC | Sin conectar |

Este proyecto usa el puerto TTL `/dev/serial0`. No se deben conectar RX y TX
del calentador a los bornes RS-485 A+/B- ni conectar VCC entre los equipos.

La UART TTL directa no proporciona aislamiento galvánico ni la inmunidad al
ruido de RS-485. Es útil para pruebas con cable corto. Para una instalación
definitiva junto a un calentador de 45 kW se recomienda una interfaz aislada y
las protecciones eléctricas correspondientes.

## Configuración de comunicación

- Calentador: `F-02 = 230` para habilitar el control por comunicación.
- Dirección del calentador: `F-03 = 1`.
- Protocolo: Modbus RTU.
- UART: 9600 baudios, 8 bits, sin paridad y 1 bit de parada (`9600 8N1`).
- Puerto Linux: `/dev/serial0`.
- Registro `0`: marcha, usando `1` para encender y `0` para apagar.
- Registro `5`: potencia, con valores entre `0` y `230`.

## Instalación

Los archivos están instalados en el PLC en:

```text
/home/RaspPLC1/Ensayos/calentador_plc
```

Para instalar las dependencias en otro equipo:

```bash
cd /home/RaspPLC1/Ensayos/calentador_plc
python3 -m pip install -r requirements.txt
```

El usuario que ejecute el programa debe tener acceso a `/dev/serial0`,
normalmente mediante el grupo `dialout`.

## Ejecución

Realizar una sola lectura y mostrar el resultado como JSON:

```bash
cd /home/RaspPLC1/Ensayos/calentador_plc
python3 control_calentador_plc.py --once
```

Iniciar el controlador y la página web:

```bash
cd /home/RaspPLC1/Ensayos/calentador_plc
python3 control_calentador_plc.py
```

La página queda disponible en:

```text
http://192.168.0.157:8080/
```

El servidor escucha en `0.0.0.0:8080`. En el PLC probado, el firewall permite
ese puerto solamente desde la red local `192.168.0.0/24`.

Actualmente el proceso se inicia manualmente y no está configurado como
servicio de arranque. Si se reinicia el PLC, hay que volver a ejecutar el
programa o instalar posteriormente una unidad `systemd`.

## API HTTP

Estado del equipo:

```http
GET /api/status
```

Comprobar que la comunicación está activa:

```http
GET /health
```

Encender:

```http
POST /api/control
Content-Type: application/json

{"action":"on"}
```

Apagar:

```http
POST /api/control
Content-Type: application/json

{"action":"off"}
```

Cambiar la potencia:

```http
POST /api/control
Content-Type: application/json

{"action":"power","power":100}
```

## Uso desde la lógica del PLC

La clase `HeaterController` no depende de Flask. La página web es solamente un
cliente de esta misma interfaz; la lógica de control puede llamar directamente
a sus métodos:

### Programa autónomo sin página web

No es necesario iniciar Flask ni realizar peticiones HTTP. El programa debe
importar `HeaterController`, crear una sola instancia y conservarla durante
toda la ejecución. El ciclo normal es:

1. Crear `HeaterController()` para abrir `/dev/serial0` a 9600 8N1.
2. Configurar la potencia con `set_power()` cuando sea necesario.
3. Evaluar las entradas, sensores, temporizadores y condiciones del PLC.
4. Llamar a `turn_on()` cuando aparece una solicitud de marcha.
5. Mientras continúe la solicitud, ejecutar `keep_alive_if_due()` varias veces
   por segundo. El método solamente transmite cuando corresponde.
6. Llamar inmediatamente a `turn_off()` cuando desaparece la solicitud o surge
   una condición de seguridad.
7. Consultar `read_status()` para comprobar marcha real, alarma y mediciones.
8. Al terminar el programa, apagar el equipo y ejecutar `close()`.

Ejemplo de estructura para un programa autónomo:

```python
import time

from control_calentador_plc import HeaterController


heater = HeaterController()
heater.set_power(100)

try:
    while True:
        # Sustituir por la lectura real de entradas, sensores o condiciones.
        solicitud_de_marcha = leer_condiciones_del_plc()

        if solicitud_de_marcha and not heater.requested_on:
            heater.turn_on()
        elif not solicitud_de_marcha and heater.requested_on:
            heater.turn_off()

        # Debe ejecutarse con frecuencia mientras exista una orden de marcha.
        heater.keep_alive_if_due()

        estado = heater.read_status()
        usar_estado_en_la_logica(estado)
        time.sleep(0.1)
finally:
    if heater.requested_on:
        heater.turn_off()
    heater.close()
```

Este ejemplo no inicia ningún servidor web. `leer_condiciones_del_plc()` debe
reemplazarse por la lógica real que lea las entradas y demás señales del
Industrial Shields. `usar_estado_en_la_logica()` puede generar alarmas, cambiar
salidas o detener el proceso según `estado.online`, `estado.switch` y
`estado.alarm`.

### Comunicación Modbus realizada por la clase

Para un programa futuro que prefiera implementar el protocolo directamente, la
comunicación utilizada es:

| Operación | Función Modbus | Registro | Valor |
|---|---:|---:|---:|
| Leer estado | `03` | Desde `0` | 6 registros |
| Encender | `06` | `0` | `1` |
| Apagar | `06` | `0` | `0` |
| Configurar potencia | `06` | `5` | `0` a `230` |
| Renovar marcha | `06` | `0` | `1` cada 4 segundos |

La dirección de esclavo es `1`, salvo que se cambie `F-03`. La biblioteca
`minimalmodbus` forma las tramas RTU, calcula y verifica el CRC y controla los
tiempos de la UART. Por eso, para la lógica futura se recomienda usar los
métodos de `HeaterController` en lugar de construir manualmente las tramas.

Enviar `turn_on()` una sola vez no es suficiente: si el registro de marcha no
se renueva antes de cinco segundos, el calentador se detiene. La aplicación web
y el ejemplo autónomo resuelven esta condición mediante
`keep_alive_if_due()`.

Métodos principales:

| Método | Función |
|---|---|
| `turn_on()` | Envía la orden de encendido y habilita la renovación |
| `turn_off()` | Cancela la renovación y envía el apagado |
| `set_power(valor)` | Escribe una potencia entera entre 0 y 230 |
| `keep_alive_if_due()` | Renueva la marcha si transcurrieron cuatro segundos |
| `read_status()` | Devuelve un objeto `HeaterStatus` con las mediciones |
| `close()` | Cierra el puerto serial |

El calentador exige renovar la orden de encendido antes de cinco segundos. La
aplicación web ya lo hace desde su hilo supervisor. Un programa propio debe
llamar frecuentemente a `keep_alive_if_due()`, como muestra el ejemplo.

No se deben ejecutar simultáneamente dos procesos que intenten abrir
`/dev/serial0`. La web y la lógica de entradas/salidas deben compartir una sola
instancia de `HeaterController` dentro del mismo proceso.

## Seguridad

Un calentador de 45 kW puede producir lesiones, incendios y daños materiales.
El software no sustituye enclavamientos físicos, parada de emergencia,
protección por caudal de refrigeración, límites de temperatura ni protecciones
eléctricas. Las salidas críticas deben llevar el equipo a un estado seguro ante
la pérdida de alimentación, comunicación o ejecución del programa.

La página web actual no tiene autenticación y debe permanecer restringida a una
red local confiable. No se recomienda publicar el puerto `8080` en Internet.
