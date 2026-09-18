# Control UART del calentador de induccion de 45 kW

Firmware para ESP32 que controla el calentador por Modbus RTU sobre su puerto
UART de 3,3 V. Ofrece una pagina Wi-Fi con lectura de estado, potencia,
encendido/apagado manual y funcionamiento ciclico configurable.

## Cableado

Realizar las conexiones con ambos equipos apagados:

| Calentador | ESP32 |
|---|---|
| TX | GPIO16 / RX2 |
| RX | GPIO17 / TX2 |
| GND | GND |
| VCC | Sin conectar |

No se utiliza MAX485, divisor resistivo, GPIO4, A ni B. La conexion directa
solo es apropiada porque el puerto medido trabaja a 3,3 V. La medicion local no
demuestra aislamiento respecto de la entrada de potencia: no conecte
simultaneamente el calentador y el USB de una computadora sin aislamiento.

## Parametros del calentador

- `F-02 = 230`: escala de potencia controlada por comunicacion.
- `F-03 = 1`: direccion Modbus utilizada por este firmware.
- Comunicacion: 9600 baudios, 8 bits, sin paridad y 1 bit de parada.

## Acceso

1. Alimente el ESP32 desde una fuente segura.
2. Conecte el telefono a `Calentador-ESP32`.
3. Use la clave `calentador45`.
4. Abra `http://192.168.4.1`.

La API de estado esta disponible en `http://192.168.4.1/api/status`.
La pagina actualiza el estado en segundo plano sin recargarse, por lo que no
borra los valores que se estan escribiendo en los formularios.

## Controles

- **Potencia:** acepta valores de 0 a 230 y escribe el registro `0x0005`.
- **Encender:** aplica primero la potencia elegida y despues activa la marcha.
- **Apagar:** cancela el modo manual o ciclico y envia OFF inmediatamente.
- **Ciclo:** permite configurar segundos encendido, segundos apagado y potencia.
  Empieza por la fase encendida y repite hasta pulsar "Detener ciclo y apagar".

Mientras la salida esta solicitada, el ESP32 renueva ON cada 4 segundos porque
el manual indica que el calentador se detiene si pasan 5 segundos sin esa
orden. En cada reinicio se envia OFF y nunca se restaura automaticamente un
ciclo anterior.

## Protecciones del firmware

- Una falla al escribir ON, OFF, potencia o el latido cancela el ciclo.
- Una alarma Modbus distinta de cero provoca una orden de apagado.
- El ciclo se detiene y apaga desde un boton separado.
- Los tiempos validos son de 1 a 3600 segundos por fase.
- La pagina exige confirmacion antes del encendido manual o del inicio del ciclo.

Las protecciones del firmware son complementarias. No reemplazan contactores,
parada de emergencia, limites termicos, protecciones electricas ni aislamiento
adecuado para una maquina de 45 kW.
