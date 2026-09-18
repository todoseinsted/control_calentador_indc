#include <Arduino.h>
#include <WebServer.h>
#include <WiFi.h>

namespace {

constexpr uint32_t USB_BAUD = 115200;
constexpr uint32_t MODBUS_BAUD = 9600;
constexpr uint32_t MODBUS_RESPONSE_TIMEOUT_MS = 300;
constexpr uint32_t MODBUS_INTERBYTE_TIMEOUT_MS = 20;
constexpr uint32_t STATUS_POLL_MS = 2000;
constexpr uint32_t HEATER_KEEPALIVE_MS = 4000;

// UART directa de 3,3 V: calentador TX -> GPIO16 y calentador RX <- GPIO17.
constexpr int HEATER_RX_PIN = 16;
constexpr int HEATER_TX_PIN = 17;

constexpr uint16_t REGISTER_SWITCH = 0x0000;
constexpr uint16_t REGISTER_POWER = 0x0005;
constexpr uint16_t MAX_POWER = 230;
constexpr uint32_t MIN_CYCLE_SECONDS = 1;
constexpr uint32_t MAX_CYCLE_SECONDS = 3600;

constexpr char AP_SSID[] = "Calentador-ESP32";
constexpr char AP_PASSWORD[] = "calentador45";

HardwareSerial &heaterSerial = Serial2;
WebServer webServer(80);

uint8_t heaterAddress = 1;
uint32_t lastStatusPollMs = 0;

struct HeaterStatus {
  bool valid = false;
  uint16_t switchState = 0;
  uint16_t current = 0;
  uint16_t frequency = 0;
  uint16_t temperature = 0;
  uint16_t alarm = 0;
  uint16_t power = 0;
  bool powerAvailable = false;
  uint32_t updatedAtMs = 0;
  String error = "Todavia no se consulto el calentador";
};

enum class ControlMode : uint8_t {
  Off,
  Manual,
  Cycle,
};

enum class CyclePhase : uint8_t {
  Off,
  On,
};

struct ControllerState {
  ControlMode mode = ControlMode::Off;
  CyclePhase phase = CyclePhase::Off;
  bool outputRequested = false;
  uint16_t powerSetpoint = 100;
  uint32_t onSeconds = 20;
  uint32_t offSeconds = 10;
  uint32_t phaseStartedMs = 0;
  uint32_t lastKeepaliveMs = 0;
  String message = "Arranque seguro: control apagado";
  bool messageIsError = false;
};

HeaterStatus heaterStatus;
ControllerState controller;

uint16_t modbusCrc(const uint8_t *data, size_t length) {
  uint16_t crc = 0xFFFF;

  for (size_t i = 0; i < length; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x0001U) != 0U ? (crc >> 1U) ^ 0xA001U : crc >> 1U;
    }
  }

  return crc;
}

void discardPendingRx() {
  while (heaterSerial.available() > 0) {
    heaterSerial.read();
  }
}

size_t exchangeFrame(const uint8_t *request, size_t requestLength,
                     uint8_t *response, size_t responseCapacity) {
  discardPendingRx();
  heaterSerial.write(request, requestLength);
  heaterSerial.flush();

  size_t received = 0;
  const uint32_t startMs = millis();
  uint32_t lastByteMs = startMs;

  while ((millis() - startMs) < MODBUS_RESPONSE_TIMEOUT_MS) {
    while (heaterSerial.available() > 0) {
      const int value = heaterSerial.read();
      if (value >= 0 && received < responseCapacity) {
        response[received++] = static_cast<uint8_t>(value);
      }
      lastByteMs = millis();
    }

    if (received > 0 && (millis() - lastByteMs) >= MODBUS_INTERBYTE_TIMEOUT_MS) {
      break;
    }
    delay(1);
  }

  return received;
}

bool frameHasValidCrc(const uint8_t *frame, size_t length) {
  if (length < 4) {
    return false;
  }

  const uint16_t receivedCrc =
      static_cast<uint16_t>(frame[length - 2]) |
      (static_cast<uint16_t>(frame[length - 1]) << 8U);
  return receivedCrc == modbusCrc(frame, length - 2);
}

String responseError(const uint8_t *response, size_t responseLength) {
  if (responseLength == 0) {
    return "Sin respuesta: revise TX/RX, GND y F-03=1";
  }
  if (!frameHasValidCrc(response, responseLength)) {
    return "Respuesta con CRC incorrecto";
  }
  if (responseLength >= 5 && response[0] == heaterAddress &&
      (response[1] & 0x80U) != 0U) {
    return "Excepcion Modbus 0x" + String(response[2], HEX);
  }
  return "Formato de respuesta inesperado (" + String(responseLength) + " bytes)";
}

bool writeRegister(uint16_t registerAddress, uint16_t value, String &error) {
  uint8_t request[8] = {
      heaterAddress,
      0x06,
      static_cast<uint8_t>(registerAddress >> 8U),
      static_cast<uint8_t>(registerAddress & 0xFFU),
      static_cast<uint8_t>(value >> 8U),
      static_cast<uint8_t>(value & 0xFFU),
      0,
      0,
  };

  const uint16_t crc = modbusCrc(request, 6);
  request[6] = static_cast<uint8_t>(crc & 0xFFU);
  request[7] = static_cast<uint8_t>(crc >> 8U);

  uint8_t response[16] = {};
  const size_t responseLength =
      exchangeFrame(request, sizeof(request), response, sizeof(response));

  if (responseLength != sizeof(request) ||
      !frameHasValidCrc(response, responseLength) ||
      memcmp(request, response, sizeof(request)) != 0) {
    error = responseError(response, responseLength);
    return false;
  }
  return true;
}

bool readRegisters(uint16_t startAddress, uint16_t registerCount,
                   uint16_t *values, String &error) {
  uint8_t request[8] = {
      heaterAddress,
      0x03,
      static_cast<uint8_t>(startAddress >> 8U),
      static_cast<uint8_t>(startAddress & 0xFFU),
      static_cast<uint8_t>(registerCount >> 8U),
      static_cast<uint8_t>(registerCount & 0xFFU),
      0,
      0,
  };

  const uint16_t crc = modbusCrc(request, 6);
  request[6] = static_cast<uint8_t>(crc & 0xFFU);
  request[7] = static_cast<uint8_t>(crc >> 8U);

  uint8_t response[32] = {};
  const size_t responseLength =
      exchangeFrame(request, sizeof(request), response, sizeof(response));
  const size_t expectedLength = 3 + registerCount * 2 + 2;

  if (responseLength != expectedLength || response[0] != heaterAddress ||
      response[1] != 0x03 || response[2] != registerCount * 2 ||
      !frameHasValidCrc(response, responseLength)) {
    error = responseError(response, responseLength);
    return false;
  }

  for (size_t i = 0; i < registerCount; ++i) {
    values[i] = (static_cast<uint16_t>(response[3 + i * 2]) << 8U) |
                response[4 + i * 2];
  }
  return true;
}

const __FlashStringHelper *alarmText(uint16_t alarm) {
  switch (alarm) {
    case 0x00: return F("Normal");
    case 0x01: return F("Sensor de corriente o bobina (E1)");
    case 0x02: return F("Tension baja (E2)");
    case 0x03: return F("Sensor de temperatura de bobina (E3)");
    case 0x04: return F("Temperatura IGBT alta (E4)");
    case 0x05: return F("Temperatura ambiente baja (E5)");
    case 0x06: return F("Falla IGBT (E6)");
    case 0x08: return F("Sensor Hall (E8)");
    case 0x09: return F("Sin carga (E9)");
    case 0x0A: return F("Sensor de temperatura de bobina (EA)");
    case 0x0C: return F("Bobina (EC)");
    default: return F("Codigo no documentado");
  }
}

void updateHeaterStatus() {
  uint16_t values[6] = {};
  String error;

  // El manual enumera seis registros, pero uno de sus ejemplos solicita cinco.
  if (readRegisters(0x0000, 6, values, error)) {
    heaterStatus.powerAvailable = true;
  } else if (readRegisters(0x0000, 5, values, error)) {
    heaterStatus.powerAvailable = false;
  } else {
    heaterStatus.valid = false;
    heaterStatus.error = error;
    heaterStatus.updatedAtMs = millis();
    return;
  }

  heaterStatus.valid = true;
  heaterStatus.switchState = values[0];
  heaterStatus.current = values[1];
  heaterStatus.frequency = values[2];
  heaterStatus.temperature = values[3];
  heaterStatus.alarm = values[4];
  heaterStatus.power = heaterStatus.powerAvailable ? values[5] : 0;
  heaterStatus.error = "";
  heaterStatus.updatedAtMs = millis();
}

void setMessage(const String &message, bool isError = false) {
  controller.message = message;
  controller.messageIsError = isError;
}

bool commandOff(String &error) {
  // Se cancela primero el latido local. Si el telegrama se pierde, el watchdog
  // de cinco segundos documentado por el fabricante tambien detiene el equipo.
  controller.outputRequested = false;
  return writeRegister(REGISTER_SWITCH, 0, error);
}

void failSafeStop(const String &reason) {
  controller.mode = ControlMode::Off;
  controller.phase = CyclePhase::Off;
  controller.outputRequested = false;
  String ignoredError;
  writeRegister(REGISTER_SWITCH, 0, ignoredError);
  setMessage("Apagado de seguridad: " + reason, true);
}

bool applyPower(uint16_t power, String &error) {
  if (!writeRegister(REGISTER_POWER, power, error)) {
    return false;
  }
  controller.powerSetpoint = power;
  return true;
}

bool commandOn(String &error) {
  if (!applyPower(controller.powerSetpoint, error)) {
    controller.outputRequested = false;
    return false;
  }
  if (!writeRegister(REGISTER_SWITCH, 1, error)) {
    controller.outputRequested = false;
    return false;
  }
  controller.outputRequested = true;
  controller.lastKeepaliveMs = millis();
  return true;
}

void manualOn() {
  controller.mode = ControlMode::Off;
  controller.phase = CyclePhase::Off;

  String error;
  if (!commandOn(error)) {
    failSafeStop("no se pudo encender: " + error);
    return;
  }

  controller.mode = ControlMode::Manual;
  setMessage("Encendido manual confirmado");
}

void turnEverythingOff(const String &successMessage) {
  controller.mode = ControlMode::Off;
  controller.phase = CyclePhase::Off;

  String error;
  if (!commandOff(error)) {
    setMessage("Se cancelo el control local, pero fallo la orden OFF: " + error, true);
    return;
  }
  setMessage(successMessage);
}

void startCycle() {
  controller.mode = ControlMode::Off;
  controller.phase = CyclePhase::Off;

  String error;
  if (!commandOn(error)) {
    failSafeStop("no se pudo iniciar el ciclo: " + error);
    return;
  }

  controller.mode = ControlMode::Cycle;
  controller.phase = CyclePhase::On;
  controller.phaseStartedMs = millis();
  setMessage("Ciclo iniciado en fase encendida");
}

uint32_t phaseDurationMs() {
  const uint32_t seconds =
      controller.phase == CyclePhase::On ? controller.onSeconds : controller.offSeconds;
  return seconds * 1000UL;
}

uint32_t phaseRemainingSeconds() {
  if (controller.mode != ControlMode::Cycle) {
    return 0;
  }
  const uint32_t elapsed = millis() - controller.phaseStartedMs;
  const uint32_t duration = phaseDurationMs();
  if (elapsed >= duration) {
    return 0;
  }
  return (duration - elapsed + 999UL) / 1000UL;
}

void serviceController() {
  const uint32_t now = millis();

  if (controller.mode == ControlMode::Cycle &&
      (now - controller.phaseStartedMs) >= phaseDurationMs()) {
    String error;
    if (controller.phase == CyclePhase::On) {
      if (!commandOff(error)) {
        failSafeStop("fallo la transicion a apagado: " + error);
        return;
      }
      controller.phase = CyclePhase::Off;
      controller.phaseStartedMs = millis();
      setMessage("Ciclo en fase apagada");
    } else {
      if (!commandOn(error)) {
        failSafeStop("fallo la transicion a encendido: " + error);
        return;
      }
      controller.phase = CyclePhase::On;
      controller.phaseStartedMs = millis();
      setMessage("Ciclo en fase encendida");
    }
  }

  if (controller.outputRequested &&
      (millis() - controller.lastKeepaliveMs) >= HEATER_KEEPALIVE_MS) {
    String error;
    if (!writeRegister(REGISTER_SWITCH, 1, error)) {
      failSafeStop("fallo el latido de marcha: " + error);
      return;
    }
    controller.lastKeepaliveMs = millis();
  }

  if (controller.outputRequested && heaterStatus.valid && heaterStatus.alarm != 0) {
    failSafeStop("alarma " + String(heaterStatus.alarm));
  }
}

bool parseUnsignedArg(const char *name, uint32_t minimum, uint32_t maximum,
                      uint32_t &value) {
  if (!webServer.hasArg(name)) {
    return false;
  }

  const String text = webServer.arg(name);
  if (text.length() == 0) {
    return false;
  }
  for (size_t i = 0; i < text.length(); ++i) {
    if (!isDigit(text[i])) {
      return false;
    }
  }

  const uint32_t parsed = static_cast<uint32_t>(strtoul(text.c_str(), nullptr, 10));
  if (parsed < minimum || parsed > maximum) {
    return false;
  }
  value = parsed;
  return true;
}

String controlModeText() {
  if (controller.mode == ControlMode::Manual) {
    return "Manual encendido";
  }
  if (controller.mode == ControlMode::Cycle) {
    return controller.phase == CyclePhase::On ? "Ciclo: encendido" : "Ciclo: apagado";
  }
  return "Apagado";
}

String statusJson() {
  String json;
  json.reserve(520);
  json += F("{\"online\":");
  json += heaterStatus.valid ? F("true") : F("false");
  json += F(",\"address\":");
  json += heaterAddress;
  json += F(",\"control_mode\":\"");
  json += controlModeText();
  json += F("\",\"requested_on\":");
  json += controller.outputRequested ? F("true") : F("false");
  json += F(",\"power_setpoint\":");
  json += controller.powerSetpoint;
  json += F(",\"cycle_on_s\":");
  json += controller.onSeconds;
  json += F(",\"cycle_off_s\":");
  json += controller.offSeconds;
  json += F(",\"phase_remaining_s\":");
  json += phaseRemainingSeconds();

  if (heaterStatus.valid) {
    json += F(",\"switch\":");
    json += heaterStatus.switchState;
    json += F(",\"current_raw\":");
    json += heaterStatus.current;
    json += F(",\"frequency_raw\":");
    json += heaterStatus.frequency;
    json += F(",\"temperature_raw\":");
    json += heaterStatus.temperature;
    json += F(",\"alarm\":");
    json += heaterStatus.alarm;
    json += F(",\"power_raw\":");
    if (heaterStatus.powerAvailable) {
      json += heaterStatus.power;
    } else {
      json += F("null");
    }
  } else {
    json += F(",\"error\":\"");
    json += heaterStatus.error;
    json += '"';
  }

  json += '}';
  return json;
}

String statusPage() {
  String html;
  html.reserve(7200);
  html += F(
      "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
      "<meta name='viewport' content='width=device-width,initial-scale=1'>"
      "<title>Calentador 45 kW</title><style>"
      "*{box-sizing:border-box}body{font-family:system-ui;background:#101820;color:#eef2f5;"
      "margin:0;padding:20px}.card{max-width:720px;margin:auto;background:#192631;"
      "border:1px solid #334655;border-radius:18px;padding:22px;box-shadow:0 12px 35px #0006}"
      "h1{font-size:1.5rem;margin:0 0 6px}h2{font-size:1.05rem;margin:26px 0 10px}"
      ".ok{color:#9cdbbc}.bad{color:#ff9c93}.msg{padding:11px 13px;border-radius:10px;"
      "background:#101820;margin-top:14px}.grid{display:grid;grid-template-columns:repeat(2,1fr);"
      "gap:12px;margin-top:18px}.item{background:#101820;border-radius:12px;padding:14px}"
      ".label{color:#9dafbd;font-size:.77rem;text-transform:uppercase}.value{font-size:1.3rem;"
      "margin-top:4px}.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:end}"
      "form{margin:0}.panel{background:#101820;border-radius:12px;padding:15px;margin-top:10px}"
      "label{display:block;color:#b9c6cf;font-size:.85rem}input{width:110px;margin-top:5px;"
      "padding:10px;border-radius:8px;border:1px solid #526575;background:#192631;color:#fff;"
      "font-size:1rem}button{border:0;border-radius:9px;padding:11px 16px;font-weight:700;"
      "cursor:pointer}.on{background:#31b875;color:#071a10}.off{background:#e5534b;color:#fff}"
      ".secondary{background:#d3a83d;color:#171204}.note{color:#9dafbd;font-size:.88rem;"
      "line-height:1.45;margin-top:18px}@media(max-width:520px){.grid{grid-template-columns:1fr}"
      ".controls{align-items:stretch}.controls form,.controls button{width:100%}}"
      "</style></head><body><main class='card'><h1>Calentador de induccion</h1>"
      "<div class='ok'>Control Modbus UART - direccion 1</div>");

  html += F("<div id='live' class='msg'>Actualizando estado...</div>");

  html += F("<div class='msg ");
  html += controller.messageIsError ? F("bad'>") : F("ok'>");
  html += controller.message;
  html += F("</div>");

  if (!heaterStatus.valid) {
    html += F("<div class='item' style='margin-top:18px'><div class='label'>Comunicacion</div>"
              "<div class='value bad'>Sin respuesta</div><p>");
    html += heaterStatus.error;
    html += F("</p></div>");
  } else {
    html += F("<div class='grid'><div class='item'><div class='label'>Marcha informada</div>"
              "<div class='value'>");
    html += heaterStatus.switchState == 1 ? F("Encendido") : F("Apagado");
    html += F("</div></div><div class='item'><div class='label'>Control solicitado</div>"
              "<div class='value'>");
    html += controlModeText();
    if (controller.mode == ControlMode::Cycle) {
      html += F(" - ");
      html += phaseRemainingSeconds();
      html += F(" s");
    }
    html += F("</div></div><div class='item'><div class='label'>Alarma</div><div class='value'>");
    html += alarmText(heaterStatus.alarm);
    html += F("</div></div><div class='item'><div class='label'>Corriente (crudo)</div>"
              "<div class='value'>");
    html += heaterStatus.current;
    html += F("</div></div><div class='item'><div class='label'>Frecuencia (crudo)</div>"
              "<div class='value'>");
    html += heaterStatus.frequency;
    html += F("</div></div><div class='item'><div class='label'>Temperatura (crudo)</div>"
              "<div class='value'>");
    html += heaterStatus.temperature;
    html += F("</div></div><div class='item'><div class='label'>Potencia informada</div>"
              "<div class='value'>");
    if (heaterStatus.powerAvailable) {
      html += heaterStatus.power;
    } else {
      html += F("No disponible");
    }
    html += F("</div></div></div>");
  }

  html += F("<h2>Potencia</h2><div class='panel'><form class='controls' method='post' "
            "action='/power'><label>Consigna 0 a 230<input name='value' type='number' min='0' "
            "max='230' required value='");
  html += controller.powerSetpoint;
  html += F("'></label><button class='secondary' type='submit'>Guardar potencia</button>"
            "</form></div>");

  html += F("<h2>Control manual</h2><div class='panel controls'>"
            "<form method='post' action='/on' onsubmit=\"return confirm('¿Encender el calentador?')\">"
            "<button class='on' type='submit'>Encender</button></form>"
            "<form method='post' action='/off'><button class='off' type='submit'>Apagar</button>"
            "</form></div>");

  html += F("<h2>Encendido y apagado ciclico</h2><div class='panel'><form method='post' "
            "action='/cycle/start' onsubmit=\"return confirm('¿Iniciar el ciclo automatico?')\">"
            "<div class='controls'><label>Encendido (s)<input name='on_s' type='number' min='1' "
            "max='3600' required value='");
  html += controller.onSeconds;
  html += F("'></label><label>Apagado (s)<input name='off_s' type='number' min='1' max='3600' "
            "required value='");
  html += controller.offSeconds;
  html += F("'></label><label>Potencia<input name='power' type='number' min='0' max='230' "
            "required value='");
  html += controller.powerSetpoint;
  html += F("'></label><button class='on' type='submit'>Iniciar ciclo</button></div></form>"
            "<form method='post' action='/cycle/stop' style='margin-top:10px'>"
            "<button class='off' type='submit'>Detener ciclo y apagar</button></form></div>");

  html += F("<p class='note'>La orden de marcha se renueva cada 4 segundos. Al detener el ciclo, "
            "reiniciar el ESP32, detectar una alarma o fallar una escritura, el control queda apagado. "
            "F-02 debe estar en 230 y F-03 en 1.</p>"
            "<script>"
            "async function actualiza(){const e=document.getElementById('live');try{"
            "const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();"
            "if(!d.online){e.className='msg bad';e.textContent='Sin comunicacion: '+d.error;return;}"
            "let s='Marcha: '+(d.switch===1?'ENCENDIDO':'apagado')+' | Control: '+d.control_mode"
            "+' | Corriente: '+d.current_raw+' | Frecuencia: '+d.frequency_raw"
            "+' | Temperatura: '+d.temperature_raw+' | Potencia: '+d.power_raw;"
            "if(d.control_mode.startsWith('Ciclo'))s+=' | Restan '+d.phase_remaining_s+' s';"
            "e.className='msg ok';e.textContent=s;}catch(x){e.className='msg bad';"
            "e.textContent='No se pudo actualizar el estado';}}"
            "actualiza();setInterval(actualiza,2000);"
            "</script></main></body></html>");
  return html;
}

void redirectHome() {
  webServer.sendHeader("Location", "/", true);
  webServer.send(303, "text/plain", "");
}

void handleRoot() {
  webServer.send(200, "text/html; charset=utf-8", statusPage());
}

void handleApiStatus() {
  webServer.send(200, "application/json; charset=utf-8", statusJson());
}

void handleSetPower() {
  uint32_t value = 0;
  if (!parseUnsignedArg("value", 0, MAX_POWER, value)) {
    setMessage("Potencia invalida: use 0 a 230", true);
    redirectHome();
    return;
  }

  String error;
  if (applyPower(static_cast<uint16_t>(value), error)) {
    setMessage("Potencia configurada en " + String(value));
  } else {
    setMessage("No se pudo configurar potencia: " + error, true);
  }
  redirectHome();
}

void handleManualOn() {
  manualOn();
  redirectHome();
}

void handleManualOff() {
  turnEverythingOff("Apagado manual confirmado");
  redirectHome();
}

void handleStartCycle() {
  uint32_t onSeconds = 0;
  uint32_t offSeconds = 0;
  uint32_t power = 0;
  if (!parseUnsignedArg("on_s", MIN_CYCLE_SECONDS, MAX_CYCLE_SECONDS, onSeconds) ||
      !parseUnsignedArg("off_s", MIN_CYCLE_SECONDS, MAX_CYCLE_SECONDS, offSeconds) ||
      !parseUnsignedArg("power", 0, MAX_POWER, power)) {
    setMessage("Parametros de ciclo invalidos", true);
    redirectHome();
    return;
  }

  controller.onSeconds = onSeconds;
  controller.offSeconds = offSeconds;
  controller.powerSetpoint = static_cast<uint16_t>(power);
  startCycle();
  redirectHome();
}

void handleStopCycle() {
  turnEverythingOff("Ciclo detenido y calentador apagado");
  redirectHome();
}

void handleNotFound() {
  redirectHome();
}

void serviceConsole() {
  static String input;

  while (Serial.available() > 0) {
    const char ch = static_cast<char>(Serial.read());
    if (ch == '\r' || ch == '\n') {
      input.trim();
      input.toLowerCase();
      if (input == "status") {
        updateHeaterStatus();
        Serial.println(statusJson());
      } else if (input == "off") {
        turnEverythingOff("Apagado solicitado por consola");
        Serial.println(controller.message);
      } else if (input.length() > 0) {
        Serial.println(F("Comandos serie disponibles: status, off"));
      }
      input = "";
    } else if (ch >= 32 && ch <= 126 && input.length() < 32) {
      input += ch;
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(USB_BAUD);
  pinMode(HEATER_RX_PIN, INPUT_PULLUP);
  heaterSerial.begin(MODBUS_BAUD, SERIAL_8N1, HEATER_RX_PIN, HEATER_TX_PIN);
  delay(300);

  WiFi.mode(WIFI_AP);
  const bool accessPointStarted = WiFi.softAP(AP_SSID, AP_PASSWORD, 1, false, 4);

  webServer.on("/", HTTP_GET, handleRoot);
  webServer.on("/api/status", HTTP_GET, handleApiStatus);
  webServer.on("/power", HTTP_POST, handleSetPower);
  webServer.on("/on", HTTP_POST, handleManualOn);
  webServer.on("/off", HTTP_POST, handleManualOff);
  webServer.on("/cycle/start", HTTP_POST, handleStartCycle);
  webServer.on("/cycle/stop", HTTP_POST, handleStopCycle);
  webServer.onNotFound(handleNotFound);
  webServer.begin();

  Serial.println();
  Serial.println(F("Control UART directo del calentador."));
  Serial.println(F("Calentador TX -> GPIO16; calentador RX <- GPIO17; GND comun."));
  Serial.print(F("Wi-Fi: "));
  Serial.println(accessPointStarted ? AP_SSID : "ERROR AL CREAR RED");
  Serial.print(F("Clave: "));
  Serial.println(AP_PASSWORD);
  Serial.print(F("Pagina: http://"));
  Serial.println(WiFi.softAPIP());

  // Orden segura en cada arranque. No se restaura automaticamente ningun ciclo.
  String startupError;
  if (!commandOff(startupError)) {
    setMessage("Arranque apagado; sin confirmacion del calentador: " + startupError, true);
  }
  updateHeaterStatus();
  lastStatusPollMs = millis();
}

void loop() {
  webServer.handleClient();
  serviceConsole();
  serviceController();

  const uint32_t now = millis();
  if ((now - lastStatusPollMs) >= STATUS_POLL_MS) {
    updateHeaterStatus();
    lastStatusPollMs = millis();
  }
}
