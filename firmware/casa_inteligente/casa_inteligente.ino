/*
 * casa_inteligente.ino — Firmware ESP32 de "Casa Inteligente en Caja".
 *
 * Librerías (Gestor de Librerías del Arduino IDE):
 *   - Adafruit SSD1306, Adafruit GFX Library
 *   - DHT sensor library (y Adafruit Unified Sensor, que pide como dependencia)
 *   - ArduinoJson 7.x (Benoit Blanchon)
 * Placa: "ESP32 Dev Module" (paquete esp32 de Espressif, 2.x o 3.x).
 *
 * Todo el firmware es no bloqueante: no hay delay(); cada tarea se ejecuta
 * cuando le toca según millis(). Las secciones del archivo siguen el orden:
 *   estado -> actuadores -> sensores -> lógica autónoma -> botones ->
 *   pantalla/indicadores -> Wi-Fi -> API REST -> setup/loop.
 *
 * API REST (la misma que usa pc/esp32_client.py; ver su cabecera):
 *   POST /luz {zona, estado}   POST /ventilador {estado}   POST /seguridad {estado}
 *   POST /config {temp_umbral} GET /estado
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <DHT.h>
#include <ArduinoJson.h>

#include "config.h"

// ===========================================================================
// Tipos y estado global
// ===========================================================================

enum Zona : uint8_t { ZONA_SALA = 0, ZONA_PATIO, ZONA_FACHADA, NUM_ZONAS };

const char* const NOMBRES_ZONA[NUM_ZONAS] = {"sala", "patio", "fachada"};
const uint8_t PINES_LED[NUM_ZONAS] = {PIN_LED_SALA, PIN_LED_PATIO, PIN_LED_FACHADA};

// Nivel lógico que enciende / apaga el relé según el módulo.
const uint8_t RELE_ON = RELE_ACTIVO_BAJO ? LOW : HIGH;
const uint8_t RELE_OFF = RELE_ACTIVO_BAJO ? HIGH : LOW;

struct Evento {
  uint32_t id;
  const char* tipo;   // "movimiento" u "obstaculo" (literales, viven siempre)
  uint32_t ms;        // millis() cuando ocurrió
};

struct Boton {
  uint8_t pin;
  bool lectura;       // última lectura cruda
  bool estable;       // último nivel estable (HIGH = suelto, por el pull-up)
  uint32_t cambio;    // cuándo cambió la lectura cruda por última vez
};

struct EstadoCasa {
  float temperatura = NAN;
  float humedad = NAN;
  float luz = 0;                       // 0..4095, mayor = más luz (ya filtrada)
  bool esDeNoche = false;
  bool movimiento = false;             // lectura actual del PIR
  bool obstaculo = false;              // lectura actual del sensor IR
  bool luces[NUM_ZONAS] = {false, false, false};
  bool luzAuto[NUM_ZONAS] = {false, false, false};   // encendida por la lógica autónoma
  bool ventilador = false;
  bool seguridad = false;
  bool alarma = false;
  bool modoAuto = true;
  bool calor = false;                  // estado del termostato con histéresis
  float tempOn = TEMP_UMBRAL_C;
  float tempOff = TEMP_UMBRAL_C - TEMP_HISTERESIS_C;
};

EstadoCasa casa;

Evento eventos[MAX_EVENTOS];            // buffer circular
uint8_t numEventos = 0;
uint8_t cabezaEventos = 0;
uint32_t ultimoEventoId = 0;

Boton botones[4] = {
  {PIN_BTN_MODO, HIGH, HIGH, 0},
  {PIN_BTN_SEGURIDAD, HIGH, HIGH, 0},
  {PIN_BTN_LUCES, HIGH, HIGH, 0},
  {PIN_BTN_VENTILADOR, HIGH, HIGH, 0},
};
enum IndiceBoton : uint8_t { BTN_MODO = 0, BTN_SEGURIDAD, BTN_LUCES, BTN_VENTILADOR };

Adafruit_SSD1306 pantalla(OLED_ANCHO, OLED_ALTO, &Wire, -1);
DHT dht(PIN_DHT, DHT_TIPO);
WebServer servidor(80);

bool hayPantalla = false;
bool wifiOk = false;
bool servidorActivo = false;
String ipTexto = "";
uint32_t ultimoIntentoWifi = 0;
bool ipBootMostrada = false;
bool ipBootActiva = false;
uint32_t ipBootInicio = 0;

uint32_t alarmaInicio = 0;
bool pitidoActivo = false;
uint32_t pitidoInicio = 0;
uint16_t pitidoDuracion = 0;

uint32_t ultimoMovimiento = 0;   // para apagar las luces automáticas
bool pirListo = false;           // true cuando terminó el calentamiento del PIR
uint8_t fallosDHT = 0;

// ===========================================================================
// Utilidades
// ===========================================================================

// True (y reinicia el temporizador) cuando pasó `intervalo` desde `ultimo`.
// La resta sin signo funciona aunque millis() dé la vuelta a los 49 días.
bool cadaMs(uint32_t& ultimo, uint32_t intervalo, uint32_t ahora) {
  if (ahora - ultimo >= intervalo) {
    ultimo = ahora;
    return true;
  }
  return false;
}

bool algunaLuzEncendida() {
  for (uint8_t z = 0; z < NUM_ZONAS; z++) {
    if (casa.luces[z]) return true;
  }
  return false;
}

// ===========================================================================
// Actuadores
// ===========================================================================

void pitido(uint16_t ms) {
  pitidoActivo = true;
  pitidoInicio = millis();
  pitidoDuracion = ms;
}

// automatica=false: la orden viene de la voz o de un botón; el usuario "toma el
// control" de la luz y la lógica autónoma ya no la apagará.
void setLuz(Zona z, bool encendida, bool automatica) {
  casa.luces[z] = encendida;
  casa.luzAuto[z] = encendida && automatica;
  digitalWrite(PINES_LED[z], encendida ? HIGH : LOW);
}

void setVentilador(bool encendido) {
  casa.ventilador = encendido;
  digitalWrite(PIN_RELE_VENTILADOR, encendido ? RELE_ON : RELE_OFF);
}

void setSeguridad(bool activa) {
  casa.seguridad = activa;
  if (!activa) casa.alarma = false;   // desarmar silencia la alarma
  Serial.printf("[seg] modo seguridad %s\n", activa ? "ACTIVADO" : "desactivado");
}

// Evalúa el termostato con histéresis. El ventilador solo se toca al CRUZAR un
// umbral (o si se pide `forzar`), así una orden manual no se pelea con el
// control automático: manda hasta el siguiente cruce.
void evaluarClima(bool forzar) {
  if (isnan(casa.temperatura)) return;
  bool antes = casa.calor;
  if (casa.temperatura >= casa.tempOn) casa.calor = true;
  else if (casa.temperatura <= casa.tempOff) casa.calor = false;
  if (casa.modoAuto && (casa.calor != antes || forzar)) {
    setVentilador(casa.calor);
    Serial.printf("[clima] %.1f C -> ventilador %s\n", casa.temperatura, casa.calor ? "ON" : "OFF");
  }
}

void cambiarModo(bool automatico) {
  casa.modoAuto = automatico;
  if (automatico) {
    evaluarClima(true);
  } else {
    // Las luces que había encendido la lógica pasan a ser manuales.
    for (uint8_t z = 0; z < NUM_ZONAS; z++) casa.luzAuto[z] = false;
  }
  Serial.printf("[modo] %s\n", automatico ? "AUTO" : "MANUAL");
}

// ===========================================================================
// Eventos de seguridad
// ===========================================================================

void registrarEvento(const char* tipo, uint32_t ahora) {
  ultimoEventoId++;
  eventos[cabezaEventos] = {ultimoEventoId, tipo, ahora};
  cabezaEventos = (cabezaEventos + 1) % MAX_EVENTOS;
  if (numEventos < MAX_EVENTOS) numEventos++;
  if (!casa.alarma) {
    casa.alarma = true;
    alarmaInicio = ahora;
  }
  Serial.printf("[seg] EVENTO #%lu: %s\n", (unsigned long)ultimoEventoId, tipo);
}

// ===========================================================================
// Sensores
// ===========================================================================

void leerLDR(uint32_t ahora) {
  static uint32_t ultimo = 0;
  static bool primera = true;
  if (!primera && !cadaMs(ultimo, LDR_INTERVALO_MS, ahora)) return;

  int crudo = analogRead(PIN_LDR);
  if (LDR_INVERTIDO) crudo = 4095 - crudo;

  // Media móvil exponencial: el ADC del ESP32 es ruidoso.
  casa.luz = primera ? crudo : (casa.luz * 0.8f + crudo * 0.2f);

  // Histéresis para que no oscile alrededor del umbral.
  if (primera) casa.esDeNoche = casa.luz < LDR_UMBRAL_NOCHE;
  else if (casa.esDeNoche && casa.luz > LDR_UMBRAL_NOCHE + LDR_HISTERESIS) casa.esDeNoche = false;
  else if (!casa.esDeNoche && casa.luz < LDR_UMBRAL_NOCHE - LDR_HISTERESIS) casa.esDeNoche = true;
  primera = false;
}

void leerDHT(uint32_t ahora) {
  static uint32_t ultimo = 0;
  if (!cadaMs(ultimo, DHT_INTERVALO_MS, ahora)) return;

  float t = dht.readTemperature();
  float h = dht.readHumidity();
  if (isnan(t) || isnan(h)) {
    // Un fallo suelto es normal; si se repite, se informa como "sin dato".
    if (++fallosDHT == DHT_FALLOS_MAX) {
      casa.temperatura = NAN;
      casa.humedad = NAN;
      Serial.println("[dht] sin lectura: revisa el cableado del DHT");
    }
    return;
  }
  fallosDHT = 0;
  casa.temperatura = t;
  casa.humedad = h;
  evaluarClima(false);
}

void leerPresencia(uint32_t ahora) {
  static uint32_t ultimoEvtPir = 0 - EVENTO_ENFRIAMIENTO_MS;   // permite el primer evento
  static uint32_t ultimoEvtObs = 0 - EVENTO_ENFRIAMIENTO_MS;

  if (!pirListo && ahora >= PIR_CALENTAMIENTO_MS) {
    pirListo = true;
    Serial.println("[pir] calentamiento terminado");
  }

  bool pir = digitalRead(PIN_PIR) == HIGH;
  bool obs = digitalRead(PIN_OBSTACULO) == (OBSTACULO_ACTIVO_BAJO ? LOW : HIGH);
  bool pirSube = pir && !casa.movimiento;      // flancos de subida
  bool obsSube = obs && !casa.obstaculo;
  casa.movimiento = pir;
  casa.obstaculo = obs;

  // --- Iluminación automática: de noche, el movimiento enciende patio y fachada.
  if (pirSube && pirListo && casa.modoAuto && casa.esDeNoche) {
    const Zona zonas[2] = {ZONA_PATIO, ZONA_FACHADA};
    for (uint8_t i = 0; i < 2; i++) {
      if (!casa.luces[zonas[i]]) setLuz(zonas[i], true, true);
    }
    Serial.println("[luz] movimiento de noche: patio y fachada encendidos");
  }
  if (pir) ultimoMovimiento = ahora;

  // Se apagan solo las luces que encendió la lógica (no las que pidió el usuario).
  if (casa.luzAuto[ZONA_PATIO] || casa.luzAuto[ZONA_FACHADA]) {
    bool sinMovimiento = ahora - ultimoMovimiento >= LUZ_AUTO_ESPERA_MS;
    if (!casa.modoAuto || !casa.esDeNoche || sinMovimiento) {
      for (uint8_t z = 0; z < NUM_ZONAS; z++) {
        if (casa.luzAuto[z]) setLuz((Zona)z, false, false);
      }
      Serial.println("[luz] apagado automático");
    }
  }

  // --- Modo seguridad: solo cuentan los flancos (un PIR que se queda en HIGH
  // genera un único evento) y se respeta un tiempo de enfriamiento.
  if (casa.seguridad && pirListo) {
    if (pirSube && ahora - ultimoEvtPir >= EVENTO_ENFRIAMIENTO_MS) {
      ultimoEvtPir = ahora;
      registrarEvento("movimiento", ahora);
    }
    if (obsSube && ahora - ultimoEvtObs >= EVENTO_ENFRIAMIENTO_MS) {
      ultimoEvtObs = ahora;
      registrarEvento("obstaculo", ahora);
    }
  }
}

// ===========================================================================
// Botones físicos (mismas acciones que los comandos de voz)
// ===========================================================================

// Devuelve true una sola vez por pulsación, ya filtrada por antirrebote.
bool botonPulsado(Boton& b, uint32_t ahora) {
  bool lectura = digitalRead(b.pin);
  if (lectura != b.lectura) {
    b.lectura = lectura;
    b.cambio = ahora;
  }
  if (ahora - b.cambio >= ANTIRREBOTE_MS && lectura != b.estable) {
    b.estable = lectura;
    return lectura == LOW;   // activo en LOW
  }
  return false;
}

void leerBotones(uint32_t ahora) {
  if (botonPulsado(botones[BTN_MODO], ahora)) {
    cambiarModo(!casa.modoAuto);
    pitido(PITIDO_BOTON_MS);
  }
  if (botonPulsado(botones[BTN_SEGURIDAD], ahora)) {
    setSeguridad(!casa.seguridad);
    pitido(PITIDO_BOTON_MS);
  }
  if (botonPulsado(botones[BTN_LUCES], ahora)) {
    // Si hay alguna luz encendida las apaga todas; si no, enciende todas.
    bool encender = !algunaLuzEncendida();
    for (uint8_t z = 0; z < NUM_ZONAS; z++) setLuz((Zona)z, encender, false);
    Serial.printf("[btn] luces %s\n", encender ? "encendidas" : "apagadas");
    pitido(PITIDO_BOTON_MS);
  }
  if (botonPulsado(botones[BTN_VENTILADOR], ahora)) {
    setVentilador(!casa.ventilador);
    Serial.printf("[btn] ventilador %s\n", casa.ventilador ? "ON" : "OFF");
    pitido(PITIDO_BOTON_MS);
  }
}

// ===========================================================================
// Indicadores: buzzer, LED RGB y pantalla OLED
// ===========================================================================

void actualizarBuzzer(uint32_t ahora) {
  bool sonar = false;
  if (casa.alarma && ahora - alarmaInicio < ALARMA_BUZZER_MAX_MS) {
    sonar = (ahora / 200) % 2 == 0;                     // pitidos de 200 ms
  } else if (pitidoActivo) {
    if (ahora - pitidoInicio < pitidoDuracion) sonar = true;
    else pitidoActivo = false;
  }
  digitalWrite(PIN_BUZZER, sonar ? HIGH : LOW);          // buzzer activo
}

// Cátodo común: HIGH enciende cada color.
void setRGB(bool r, bool g, bool b) {
  digitalWrite(PIN_RGB_R, r);
  digitalWrite(PIN_RGB_G, g);
  digitalWrite(PIN_RGB_B, b);
}

// Prioridad: alarma > sin Wi-Fi > seguridad armada > modo manual > normal.
void actualizarRGB(uint32_t ahora) {
  if (casa.alarma) {
    bool on = (ahora / 150) % 2 == 0;
    setRGB(on, false, false);                  // rojo parpadeante rápido
  } else if (!wifiOk) {
    bool on = (ahora / 500) % 2 == 0;
    setRGB(on, on, false);                     // amarillo parpadeante: sin Wi-Fi
  } else if (casa.seguridad) {
    setRGB(true, false, true);                 // magenta fijo: seguridad armada
  } else if (!casa.modoAuto) {
    setRGB(false, false, true);                // azul fijo: modo manual
  } else {
    setRGB(false, true, false);                // verde fijo: todo normal (auto)
  }
}

void dibujarPantallaIP() {
  pantalla.clearDisplay();
  pantalla.setTextColor(SSD1306_WHITE);
  pantalla.setTextSize(1);
  pantalla.setCursor(0, 0);
  pantalla.println("Casa Inteligente");
  pantalla.println();
  pantalla.println("Wi-Fi conectado");
  pantalla.println("IP:");
  pantalla.setTextSize(2);
  pantalla.println(ipTexto);
  pantalla.display();
}

void dibujarPantallaEstado() {
  pantalla.clearDisplay();
  pantalla.setTextColor(SSD1306_WHITE);
  pantalla.setTextSize(1);
  pantalla.setCursor(0, 0);

  pantalla.println(wifiOk ? ipTexto : String("Wi-Fi: sin conexion"));
  if (isnan(casa.temperatura)) pantalla.println("Temp: -- C");
  else pantalla.printf("Temp: %.1f C\n", casa.temperatura);
  if (isnan(casa.humedad)) pantalla.println("Hum:  -- %");
  else pantalla.printf("Hum:  %.0f %%\n", casa.humedad);
  pantalla.printf("Luz:  %d (%s)\n", (int)lroundf(casa.luz), casa.esDeNoche ? "noche" : "dia");
  pantalla.printf("Modo: %s\n", casa.modoAuto ? "AUTO" : "MANUAL");
  pantalla.printf("Seguridad: %s\n", casa.seguridad ? "ON" : "OFF");
  if (casa.alarma) pantalla.println("!!! ALARMA !!!");
  else pantalla.printf("Vent:%s Luces:%d/%d\n", casa.ventilador ? "ON" : "OFF",
                       (casa.luces[0] + casa.luces[1] + casa.luces[2]), NUM_ZONAS);
  pantalla.display();
}

void actualizarPantalla(uint32_t ahora) {
  static uint32_t ultimo = 0;
  if (!hayPantalla || !cadaMs(ultimo, OLED_INTERVALO_MS, ahora)) return;

  if (ipBootActiva && ahora - ipBootInicio < PANTALLA_IP_MS) {
    dibujarPantallaIP();
  } else {
    ipBootActiva = false;
    dibujarPantallaEstado();
  }
}

// ===========================================================================
// Wi-Fi (reconexión automática y sin bloqueos)
// ===========================================================================

void gestionarWiFi(uint32_t ahora) {
  bool conectado = WiFi.status() == WL_CONNECTED;

  if (conectado && !wifiOk) {
    wifiOk = true;
    ipTexto = WiFi.localIP().toString();
    Serial.printf("[wifi] conectado. IP: %s  RSSI: %d dBm\n", ipTexto.c_str(), WiFi.RSSI());
    if (!servidorActivo) {
      servidor.begin();
      servidorActivo = true;
    }
    MDNS.end();
    if (MDNS.begin(NOMBRE_HOST)) MDNS.addService("http", "tcp", 80);
    if (!ipBootMostrada) {            // la IP grande se muestra solo la primera vez
      ipBootMostrada = true;
      ipBootActiva = true;
      ipBootInicio = ahora;
    }
  } else if (!conectado && wifiOk) {
    wifiOk = false;
    ultimoIntentoWifi = ahora;
    Serial.println("[wifi] conexión perdida; reintentando...");
  }

  if (!conectado && ahora - ultimoIntentoWifi >= WIFI_REINTENTO_MS) {
    ultimoIntentoWifi = ahora;
    Serial.println("[wifi] intentando conectar...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
}

// ===========================================================================
// API REST
// ===========================================================================

void responder(int codigo, const JsonDocument& doc) {
  String cuerpo;
  serializeJson(doc, cuerpo);
  servidor.send(codigo, "application/json", cuerpo);
}

void responderError(int codigo, const char* mensaje) {
  JsonDocument doc;
  doc["ok"] = false;
  doc["error"] = mensaje;
  responder(codigo, doc);
}

// Lee el cuerpo JSON de un POST. Si falla, ya envía el error 400.
bool leerJson(JsonDocument& doc) {
  if (!servidor.hasArg("plain") || servidor.arg("plain").length() == 0) {
    responderError(400, "cuerpo vacio: se esperaba JSON");
    return false;
  }
  if (deserializeJson(doc, servidor.arg("plain"))) {
    responderError(400, "JSON invalido");
    return false;
  }
  return true;
}

// Lee {"estado": true|false}. Si falta o no es booleano, envía el error 400.
bool leerEstadoBool(const JsonDocument& doc, bool& valor) {
  if (!doc["estado"].is<bool>()) {
    responderError(400, "'estado' debe ser true o false");
    return false;
  }
  valor = doc["estado"].as<bool>();
  return true;
}

int indiceZona(const char* nombre) {
  if (nombre == nullptr) return -1;
  for (uint8_t i = 0; i < NUM_ZONAS; i++) {
    if (strcmp(nombre, NOMBRES_ZONA[i]) == 0) return i;
  }
  return -1;
}

void manejarLuz() {
  JsonDocument doc;
  bool estado;
  if (!leerJson(doc) || !leerEstadoBool(doc, estado)) return;

  const char* zona = doc["zona"].is<const char*>() ? doc["zona"].as<const char*>() : nullptr;
  int idx = indiceZona(zona);
  if (idx < 0) {
    responderError(400, "zona desconocida (validas: sala, patio, fachada)");
    return;
  }
  setLuz((Zona)idx, estado, false);
  Serial.printf("[api] luz %s -> %s\n", NOMBRES_ZONA[idx], estado ? "ON" : "OFF");

  JsonDocument r;
  r["ok"] = true;
  r["zona"] = NOMBRES_ZONA[idx];
  r["estado"] = estado;
  responder(200, r);
}

void manejarVentilador() {
  JsonDocument doc;
  bool estado;
  if (!leerJson(doc) || !leerEstadoBool(doc, estado)) return;

  setVentilador(estado);
  Serial.printf("[api] ventilador -> %s\n", estado ? "ON" : "OFF");

  JsonDocument r;
  r["ok"] = true;
  r["estado"] = estado;
  responder(200, r);
}

void manejarSeguridad() {
  JsonDocument doc;
  bool estado;
  if (!leerJson(doc) || !leerEstadoBool(doc, estado)) return;

  setSeguridad(estado);

  JsonDocument r;
  r["ok"] = true;
  r["estado"] = estado;
  responder(200, r);
}

// POST /config {"temp_umbral": 30}: cambia el umbral del ventilador (no se guarda
// al reiniciar; el valor permanente se fija en config.h).
void manejarConfig() {
  JsonDocument doc;
  if (!leerJson(doc)) return;
  if (!doc["temp_umbral"].is<float>()) {
    responderError(400, "'temp_umbral' debe ser un numero");
    return;
  }
  float umbral = doc["temp_umbral"].as<float>();
  if (umbral < TEMP_UMBRAL_MIN_C || umbral > TEMP_UMBRAL_MAX_C) {
    responderError(400, "'temp_umbral' fuera de rango (15 a 45)");
    return;
  }
  casa.tempOn = umbral;
  casa.tempOff = umbral - TEMP_HISTERESIS_C;
  evaluarClima(false);
  Serial.printf("[api] umbral de temperatura: %.1f C\n", umbral);

  JsonDocument r;
  r["ok"] = true;
  r["temp_umbral"] = casa.tempOn;
  responder(200, r);
}

void manejarEstado() {
  JsonDocument d;

  // NaN (sensor sin lectura) se informa como null.
  if (isnan(casa.temperatura)) d["temperatura"] = nullptr;
  else d["temperatura"] = casa.temperatura;
  if (isnan(casa.humedad)) d["humedad"] = nullptr;
  else d["humedad"] = casa.humedad;

  d["luz"] = (int)lroundf(casa.luz);
  d["es_de_noche"] = casa.esDeNoche;
  d["movimiento"] = casa.movimiento;
  d["obstaculo"] = casa.obstaculo;

  JsonObject luces = d["luces"].to<JsonObject>();
  for (uint8_t z = 0; z < NUM_ZONAS; z++) luces[NOMBRES_ZONA[z]] = casa.luces[z];

  d["ventilador"] = casa.ventilador;
  d["seguridad"] = casa.seguridad;
  d["alarma"] = casa.alarma;
  d["modo"] = casa.modoAuto ? "auto" : "manual";
  d["temp_umbral"] = casa.tempOn;

  // Eventos de seguridad, del más antiguo al más reciente.
  JsonArray lista = d["eventos"].to<JsonArray>();
  uint8_t inicio = (cabezaEventos + MAX_EVENTOS - numEventos) % MAX_EVENTOS;
  for (uint8_t i = 0; i < numEventos; i++) {
    const Evento& e = eventos[(inicio + i) % MAX_EVENTOS];
    JsonObject o = lista.add<JsonObject>();
    o["id"] = e.id;
    o["tipo"] = e.tipo;
    o["ms"] = e.ms;
  }
  d["ultimo_evento_id"] = ultimoEventoId;
  d["uptime_ms"] = millis();
  d["rssi"] = WiFi.RSSI();

  responder(200, d);
}

void manejarNoEncontrado() {
  responderError(404, "ruta no encontrada");
}

void configurarRutas() {
  servidor.on("/luz", HTTP_POST, manejarLuz);
  servidor.on("/ventilador", HTTP_POST, manejarVentilador);
  servidor.on("/seguridad", HTTP_POST, manejarSeguridad);
  servidor.on("/config", HTTP_POST, manejarConfig);
  servidor.on("/estado", HTTP_GET, manejarEstado);
  servidor.onNotFound(manejarNoEncontrado);
}

// ===========================================================================
// setup() y loop()
// ===========================================================================

void configurarPines() {
  // Los relés se dejan apagados ANTES de pasar a salida, para que no den un
  // pulso al arrancar.
  digitalWrite(PIN_RELE_VENTILADOR, RELE_OFF);
  digitalWrite(PIN_RELE_RESERVA, RELE_OFF);
  pinMode(PIN_RELE_VENTILADOR, OUTPUT);
  pinMode(PIN_RELE_RESERVA, OUTPUT);

  for (uint8_t z = 0; z < NUM_ZONAS; z++) {
    pinMode(PINES_LED[z], OUTPUT);
    digitalWrite(PINES_LED[z], LOW);
  }
  const uint8_t salidas[] = {PIN_RGB_R, PIN_RGB_G, PIN_RGB_B, PIN_BUZZER};
  for (uint8_t pin : salidas) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
  }

  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_OBSTACULO, INPUT);   // GPIO35 no tiene pull-up: el módulo IR trae el suyo
  pinMode(PIN_LDR, INPUT);
  for (Boton& b : botones) pinMode(b.pin, INPUT_PULLUP);
}

void setup() {
  Serial.begin(115200);
  Serial.println("\n[boot] Casa Inteligente en Caja");

  configurarPines();

  Wire.begin(PIN_OLED_SDA, PIN_OLED_SCL);
  Wire.setClock(400000);   // a 100 kHz, refrescar la OLED tarda ~100 ms
  hayPantalla = pantalla.begin(SSD1306_SWITCHCAPVCC, OLED_DIRECCION);
  if (hayPantalla) {
    pantalla.clearDisplay();
    pantalla.setTextColor(SSD1306_WHITE);
    pantalla.setTextSize(1);
    pantalla.setCursor(0, 0);
    pantalla.println("Casa Inteligente");
    pantalla.println();
    pantalla.println("Conectando Wi-Fi...");
    pantalla.println(WIFI_SSID);
    pantalla.display();
  } else {
    Serial.println("[oled] no se encontró la pantalla (revisa SDA/SCL y la dirección)");
  }

  dht.begin();
  leerLDR(millis());   // primera lectura inmediata

  WiFi.persistent(false);   // no desgastar la flash guardando credenciales
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(NOMBRE_HOST);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  ultimoIntentoWifi = millis();

  configurarRutas();
  ultimoMovimiento = millis();
}

void loop() {
  uint32_t ahora = millis();

  gestionarWiFi(ahora);
  if (servidorActivo) servidor.handleClient();

  leerLDR(ahora);
  leerDHT(ahora);
  leerPresencia(ahora);
  leerBotones(ahora);

  actualizarBuzzer(ahora);
  actualizarRGB(ahora);
  actualizarPantalla(ahora);
}
