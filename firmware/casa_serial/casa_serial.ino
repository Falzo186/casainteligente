/*
 * casa_serial.ino — Casa domótica ESP32 controlada por USB (puerto serie).
 *
 * Es tu código de pruebas, con los mismos pines y el mismo comportamiento, más
 * un protocolo de líneas con ESTADO EXPLÍCITO para que la PC (Python) y la
 * interfaz gráfica no se desincronicen. Tus comandos de una letra siguen
 * funcionando (ver más abajo).
 *
 * Librerías: DFRobotDFPlayerMini, DHT sensor library (+ Adafruit Unified Sensor).
 * Serie USB: 115200 baudios, fin de línea "\n".
 *
 * ---------------------------------------------------------------------------
 * PROTOCOLO (una orden por línea, mayúsculas o minúsculas)
 * ---------------------------------------------------------------------------
 *   PING            -> PONG
 *   STATUS          -> {"t":24.5,"h":58,"pir":0,"leds":[0,0,0,0,0,0],"fan":0,
 *                       "guard":0,"alarm":0,"mp3":1,"track":0,"vol":14,"ev":0,"up":1234}
 *   LED <1-6> <0|1> -> OK LED n v      (1 Baño, 2 Cocina, 3 Comedor,
 *   LEDS <0|1>      -> OK LEDS v        4 Sala, 5 Cuarto, 6 Exterior)
 *   FAN <0|1>       -> OK FAN v
 *   GUARD <0|1>     -> OK GUARD v      (activa/desactiva el modo guardián)
 *   ALARMOFF        -> OK ALARMOFF     (apaga alarma y guardián, como tu "X")
 *   PLAY <n>        -> OK PLAY n       (n = número de archivo 0001..0030 del SD;
 *                                       la 1 es la alarma, 2..11 las canciones)
 *   STOP            -> OK STOP
 *   VOL <0-30>      -> OK VOL v
 *   HELP            -> OK HELP ...
 *   Errores         -> ERR <motivo>
 *
 * Eventos espontáneos (la PC los lee sin haber preguntado):
 *   EVT INTRUSO     el PIR detectó movimiento con el guardián activo
 *   EVT FIN <n>     terminó de sonar la pista n
 *
 * ---------------------------------------------------------------------------
 * COMANDOS DE UNA LETRA (tuyos, sin cambios): envía la letra + Enter
 *   1-6 alterna foco | 9 todos | 0 apagar todos | G guardián | X apagar alarma
 *   T temperatura | V ventilador | P PIR | A prueba de alarma | S stop | M menú
 * ---------------------------------------------------------------------------
 */

#include <Arduino.h>
#include <DFRobotDFPlayerMini.h>
#include <DHT.h>

// ---------------------------------------------------------------------------
// Pines (los de tu prueba)
// ---------------------------------------------------------------------------
const uint8_t NUM_LEDS = 6;
const int leds[NUM_LEDS] = {13, 14, 27, 26, 25, 33};
const char* const nombresLeds[NUM_LEDS] = {
  "Bano (D13)", "Cocina (D14)", "Comedor (D27)", "Sala (D26)", "Cuarto (D25)", "Exterior/Entrada (D33)"
};

const int PIN_VENTILADOR = 23;
const int PIN_PIR = 18;
const int PIN_DHT = 19;
const int PIN_MP3_RX = 16;     // RX2 <- TX del DFPlayer
const int PIN_MP3_TX = 17;     // TX2 -> RX del DFPlayer

const bool RELE_ACTIVO_BAJO = true;
#define DHTTYPE DHT11

const uint8_t PISTA_ALARMA = 1;                 // 0001.mp3
const uint8_t PISTA_MAX = 30;
const uint8_t VOLUMEN_INICIAL = 14;             // estable para evitar caídas de USB
const unsigned long INTERVALO_PARPADEO_MS = 2000;
const unsigned long INTERVALO_DHT_MS = 2000;    // el DHT11 no admite lecturas más rápidas
const size_t MAX_LINEA = 64;

// ---------------------------------------------------------------------------
// Estado
// ---------------------------------------------------------------------------
DHT dht(PIN_DHT, DHTTYPE);
DFRobotDFPlayerMini myDFPlayer;
bool mp3Listo = false;

bool estadoLeds[NUM_LEDS] = {false, false, false, false, false, false};
bool estadoVentilador = false;
bool modoGuardianActivo = false;
bool alarmaSonando = false;
bool estadoParpadeo = false;
unsigned long tiempoAnteriorParpadeo = 0;

uint8_t pistaActual = 0;        // 0 = nada sonando
uint8_t volumen = VOLUMEN_INICIAL;
uint32_t contadorEventos = 0;   // sube con cada intrusión

float temperatura = NAN;
float humedad = NAN;
unsigned long ultimaLecturaDht = 0;

String linea = "";

// ---------------------------------------------------------------------------
// Actuadores
// ---------------------------------------------------------------------------
void escribirLed(uint8_t i) {
  digitalWrite(leds[i], estadoLeds[i] ? HIGH : LOW);
}

void aplicarLeds() {
  for (uint8_t i = 0; i < NUM_LEDS; i++) escribirLed(i);
}

// Durante la alarma los focos parpadean; el estado "lógico" se conserva y se
// restaura al apagarla.
void setLed(uint8_t i, bool on) {
  estadoLeds[i] = on;
  if (!alarmaSonando) escribirLed(i);
}

void setTodosLosLeds(bool on) {
  for (uint8_t i = 0; i < NUM_LEDS; i++) setLed(i, on);
}

void cambiarTodosLosLeds(bool encender) {   // solo para el parpadeo de la alarma
  for (uint8_t i = 0; i < NUM_LEDS; i++) digitalWrite(leds[i], encender ? HIGH : LOW);
}

void setVentilador(bool encender) {
  estadoVentilador = encender;
  digitalWrite(PIN_VENTILADOR, (RELE_ACTIVO_BAJO ? !encender : encender) ? HIGH : LOW);
}

bool reproducir(uint8_t pista) {
  if (!mp3Listo) return false;
  myDFPlayer.play(pista);
  pistaActual = pista;
  return true;
}

void detener() {
  if (mp3Listo) myDFPlayer.stop();
  pistaActual = 0;
}

void setVolumen(uint8_t v) {
  volumen = v;
  if (mp3Listo) myDFPlayer.volume(v);
}

void desactivarAlarmaYGuardian() {
  modoGuardianActivo = false;
  if (alarmaSonando) detener();
  alarmaSonando = false;
  aplicarLeds();
}

void activarAlarma() {
  alarmaSonando = true;
  tiempoAnteriorParpadeo = millis();
  estadoParpadeo = true;
  cambiarTodosLosLeds(true);
  contadorEventos++;
  Serial.println(F("EVT INTRUSO"));
  if (mp3Listo) {
    myDFPlayer.loop(PISTA_ALARMA);
    pistaActual = PISTA_ALARMA;
  }
}

// ---------------------------------------------------------------------------
// Respuestas
// ---------------------------------------------------------------------------
void imprimirNumeroONulo(float v, uint8_t decimales) {
  if (isnan(v)) Serial.print(F("null"));
  else Serial.print(v, decimales);
}

void enviarEstado() {
  Serial.print(F("{\"t\":"));
  imprimirNumeroONulo(temperatura, 1);
  Serial.print(F(",\"h\":"));
  imprimirNumeroONulo(humedad, 0);
  Serial.print(F(",\"pir\":"));
  Serial.print(digitalRead(PIN_PIR) == HIGH ? 1 : 0);
  Serial.print(F(",\"leds\":["));
  for (uint8_t i = 0; i < NUM_LEDS; i++) {
    if (i) Serial.print(',');
    Serial.print(estadoLeds[i] ? 1 : 0);
  }
  Serial.print(F("],\"fan\":"));
  Serial.print(estadoVentilador ? 1 : 0);
  Serial.print(F(",\"guard\":"));
  Serial.print(modoGuardianActivo ? 1 : 0);
  Serial.print(F(",\"alarm\":"));
  Serial.print(alarmaSonando ? 1 : 0);
  Serial.print(F(",\"mp3\":"));
  Serial.print(mp3Listo ? 1 : 0);
  Serial.print(F(",\"track\":"));
  Serial.print(pistaActual);
  Serial.print(F(",\"vol\":"));
  Serial.print(volumen);
  Serial.print(F(",\"ev\":"));
  Serial.print(contadorEventos);
  Serial.print(F(",\"up\":"));
  Serial.print(millis());
  Serial.println('}');
}

void mostrarMenu() {
  Serial.println(F("[G] Guardian | [X] Apagar Alarma | [1-6] Focos | [9] Todos | [0] Apagar | [T] Temp | [V] Ventilador | [A] Audio | [S] Stop"));
}

// ---------------------------------------------------------------------------
// Comandos de una letra (los de tu prueba)
// ---------------------------------------------------------------------------
void procesarComandoLetra(char cmd) {
  cmd = toupper(cmd);

  if (cmd >= '1' && cmd <= '6') {
    uint8_t i = cmd - '1';
    setLed(i, !estadoLeds[i]);
    Serial.print(estadoLeds[i] ? F("Encendiendo ") : F("Apagando "));
    Serial.println(nombresLeds[i]);
  } else if (cmd == '9') {
    setTodosLosLeds(true);
    Serial.println(F("Todos los focos ENCENDIDOS."));
  } else if (cmd == '0') {
    setTodosLosLeds(false);
    Serial.println(F("Todos los focos APAGADOS."));
  } else if (cmd == 'G') {
    if (!modoGuardianActivo) {
      modoGuardianActivo = true;
      Serial.println(F("[GUARDIAN ACTIVADO]"));
    } else {
      desactivarAlarmaYGuardian();
      Serial.println(F("[OK] ALARMA Y MODO GUARDIAN DESACTIVADOS."));
    }
  } else if (cmd == 'X') {
    desactivarAlarmaYGuardian();
    Serial.println(F("[OK] ALARMA Y MODO GUARDIAN DESACTIVADOS."));
  } else if (cmd == 'T') {
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (isnan(t) || isnan(h)) {
      Serial.println(F("Error DHT11 (Pin D19). Verifica que 'S' vaya a D19, centro a 3V3 y '-' a GND."));
    } else {
      Serial.print(F("Temp: ")); Serial.print(t); Serial.print(F(" C | Hum: ")); Serial.print(h); Serial.println(F(" %"));
    }
  } else if (cmd == 'V') {
    setVentilador(!estadoVentilador);
    Serial.println(estadoVentilador ? F("Ventilador (D23) ENCENDIDO") : F("Ventilador (D23) APAGADO"));
  } else if (cmd == 'P') {
    Serial.println(digitalRead(PIN_PIR) == HIGH ? F("PIR: MOVIMIENTO") : F("PIR: Sin movimiento"));
  } else if (cmd == 'A') {
    if (mp3Listo) { Serial.println(F("Probando 001.mp3...")); reproducir(PISTA_ALARMA); }
    else Serial.println(F("MP3: No detectado."));
  } else if (cmd == 'S') {
    if (mp3Listo) { detener(); Serial.println(F("Audio detenido.")); }
  } else if (cmd == 'M') {
    mostrarMenu();
  }
}

// ---------------------------------------------------------------------------
// Protocolo de líneas
// ---------------------------------------------------------------------------
// Lee hasta `n` enteros separados por espacios. Devuelve true si están todos
// y no sobra nada.
bool leerEnteros(const String& texto, uint8_t n, int* salida) {
  int consumidos = 0;
  int leidos = (n == 1) ? sscanf(texto.c_str(), "%d%n", &salida[0], &consumidos)
                        : sscanf(texto.c_str(), "%d %d%n", &salida[0], &salida[1], &consumidos);
  if (leidos != n) return false;
  String sobrante = texto.substring(consumidos);
  sobrante.trim();
  return sobrante.length() == 0;
}

void responderOk(const String& texto) {
  Serial.print(F("OK "));
  Serial.println(texto);
}

void responderError(const __FlashStringHelper* motivo) {
  Serial.print(F("ERR "));
  Serial.println(motivo);
}

void procesarLinea(String texto) {
  texto.trim();
  if (texto.length() == 0) return;
  if (texto.length() == 1) {            // comando de una letra (compatibilidad)
    procesarComandoLetra(texto[0]);
    return;
  }

  texto.toUpperCase();
  int espacio = texto.indexOf(' ');
  String verbo = espacio < 0 ? texto : texto.substring(0, espacio);
  String resto = espacio < 0 ? "" : texto.substring(espacio + 1);
  resto.trim();
  int v[2] = {0, 0};

  if (verbo == "PING") {
    Serial.println(F("PONG"));
  } else if (verbo == "STATUS") {
    enviarEstado();
  } else if (verbo == "LED") {
    if (!leerEnteros(resto, 2, v) || v[0] < 1 || v[0] > NUM_LEDS || (v[1] != 0 && v[1] != 1)) {
      responderError(F("uso: LED <1-6> <0|1>"));
      return;
    }
    setLed(v[0] - 1, v[1] == 1);
    responderOk("LED " + String(v[0]) + " " + String(v[1]));
  } else if (verbo == "LEDS") {
    if (!leerEnteros(resto, 1, v) || (v[0] != 0 && v[0] != 1)) {
      responderError(F("uso: LEDS <0|1>"));
      return;
    }
    setTodosLosLeds(v[0] == 1);
    responderOk("LEDS " + String(v[0]));
  } else if (verbo == "FAN") {
    if (!leerEnteros(resto, 1, v) || (v[0] != 0 && v[0] != 1)) {
      responderError(F("uso: FAN <0|1>"));
      return;
    }
    setVentilador(v[0] == 1);
    responderOk("FAN " + String(v[0]));
  } else if (verbo == "GUARD") {
    if (!leerEnteros(resto, 1, v) || (v[0] != 0 && v[0] != 1)) {
      responderError(F("uso: GUARD <0|1>"));
      return;
    }
    if (v[0] == 1) modoGuardianActivo = true;
    else desactivarAlarmaYGuardian();
    responderOk("GUARD " + String(v[0]));
  } else if (verbo == "ALARMOFF") {
    desactivarAlarmaYGuardian();
    responderOk("ALARMOFF");
  } else if (verbo == "PLAY") {
    if (!leerEnteros(resto, 1, v) || v[0] < 1 || v[0] > PISTA_MAX) {
      responderError(F("uso: PLAY <1-30>"));
      return;
    }
    if (alarmaSonando) { responderError(F("alarma activa")); return; }
    if (!reproducir(v[0])) { responderError(F("mp3 no detectado")); return; }
    responderOk("PLAY " + String(v[0]));
  } else if (verbo == "STOP") {
    if (!mp3Listo) { responderError(F("mp3 no detectado")); return; }
    detener();
    responderOk("STOP");
  } else if (verbo == "VOL") {
    if (!leerEnteros(resto, 1, v) || v[0] < 0 || v[0] > 30) {
      responderError(F("uso: VOL <0-30>"));
      return;
    }
    setVolumen(v[0]);
    responderOk("VOL " + String(v[0]));
  } else if (verbo == "HELP") {
    responderOk("PING STATUS LED LEDS FAN GUARD ALARMOFF PLAY STOP VOL");
  } else {
    responderError(F("comando desconocido"));
  }
}

// ---------------------------------------------------------------------------
// setup() y loop()
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  Serial2.begin(9600, SERIAL_8N1, PIN_MP3_RX, PIN_MP3_TX);
  dht.begin();

  for (uint8_t i = 0; i < NUM_LEDS; i++) {
    pinMode(leds[i], OUTPUT);
    digitalWrite(leds[i], LOW);
  }
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_VENTILADOR, OUTPUT);
  setVentilador(false);

  delay(500);   // solo en el arranque
  Serial.println(F("\n=== CASA DOMOTICA ESP32 LISTA ==="));

  if (!myDFPlayer.begin(Serial2, true, false)) {
    Serial.println(F("MP3: No detectado."));
  } else {
    Serial.println(F("MP3: OK!"));
    mp3Listo = true;
    myDFPlayer.volume(volumen);
  }
  mostrarMenu();
  linea.reserve(MAX_LINEA);
}

void loop() {
  // --- Entrada serie: se acumula hasta el fin de línea ---
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (linea.length() > 0) procesarLinea(linea);
      linea = "";
    } else if (linea.length() < MAX_LINEA) {
      linea += c;
    }
  }

  unsigned long ahora = millis();

  // --- Lectura periódica del DHT (para STATUS) ---
  if (ahora - ultimaLecturaDht >= INTERVALO_DHT_MS) {
    ultimaLecturaDht = ahora;
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (!isnan(t) && !isnan(h)) {
      temperatura = t;
      humedad = h;
    }
  }

  // --- Modo guardián ---
  if (modoGuardianActivo && !alarmaSonando && digitalRead(PIN_PIR) == HIGH) {
    activarAlarma();
  }

  // --- Parpadeo de la alarma ---
  if (alarmaSonando && ahora - tiempoAnteriorParpadeo >= INTERVALO_PARPADEO_MS) {
    tiempoAnteriorParpadeo = ahora;
    estadoParpadeo = !estadoParpadeo;
    cambiarTodosLosLeds(estadoParpadeo);
  }

  // --- Avisos del DFPlayer (fin de pista) ---
  if (mp3Listo && myDFPlayer.available()) {
    if (myDFPlayer.readType() == DFPlayerPlayFinished && !alarmaSonando && pistaActual != 0) {
      Serial.print(F("EVT FIN "));
      Serial.println(pistaActual);
      pistaActual = 0;
    }
  }
}
