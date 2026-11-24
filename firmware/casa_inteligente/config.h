// config.h — Ajustes del firmware de "Casa Inteligente en Caja".
// Todo lo que puede cambiar de una instalación a otra está aquí.
#pragma once

#include <Arduino.h>

// ---------------------------------------------------------------------------
// Wi-Fi (el ESP32 solo soporta redes de 2.4 GHz)
// ---------------------------------------------------------------------------
#define WIFI_SSID      "TU_RED_WIFI"
#define WIFI_PASSWORD  "TU_CLAVE_WIFI"
#define NOMBRE_HOST    "casa-inteligente"   // también accesible como casa-inteligente.local

// ---------------------------------------------------------------------------
// Pines
// ---------------------------------------------------------------------------
constexpr uint8_t PIN_OLED_SDA = 21;
constexpr uint8_t PIN_OLED_SCL = 22;
constexpr uint8_t PIN_LDR = 34;            // solo entrada (ADC1)
constexpr uint8_t PIN_DHT = 4;
constexpr uint8_t PIN_PIR = 27;
constexpr uint8_t PIN_OBSTACULO = 35;      // solo entrada, SIN pull-up interno
constexpr uint8_t PIN_LED_SALA = 16;
constexpr uint8_t PIN_LED_PATIO = 17;
constexpr uint8_t PIN_LED_FACHADA = 18;
constexpr uint8_t PIN_RGB_R = 19;
constexpr uint8_t PIN_RGB_G = 23;
constexpr uint8_t PIN_RGB_B = 25;
constexpr uint8_t PIN_BUZZER = 26;
constexpr uint8_t PIN_RELE_VENTILADOR = 32;
constexpr uint8_t PIN_RELE_RESERVA = 33;   // sin uso por ahora (queda apagado)
constexpr uint8_t PIN_BTN_MODO = 13;
constexpr uint8_t PIN_BTN_SEGURIDAD = 14;
constexpr uint8_t PIN_BTN_LUCES = 5;
constexpr uint8_t PIN_BTN_VENTILADOR = 15;

// ---------------------------------------------------------------------------
// Polaridad de los módulos (cámbialo si algo funciona al revés)
// ---------------------------------------------------------------------------
#define RELE_ACTIVO_BAJO        true   // la mayoría de módulos de relé se activan con LOW
#define OBSTACULO_ACTIVO_BAJO   true   // la mayoría de sensores IR dan LOW al detectar
#define LDR_INVERTIDO           false  // false: más luz => lectura mayor (LDR a GND + resistencia a 3V3)
                                       // true:  más luz => lectura menor (LDR a 3V3 + resistencia a GND)
#define DHT_TIPO                22     // 11 (DHT11) o 22 (DHT22)

// ---------------------------------------------------------------------------
// OLED
// ---------------------------------------------------------------------------
constexpr uint8_t OLED_DIRECCION = 0x3C;   // algunas pantallas usan 0x3D
constexpr uint8_t OLED_ANCHO = 128;
constexpr uint8_t OLED_ALTO = 64;

// ---------------------------------------------------------------------------
// Lógica autónoma
// ---------------------------------------------------------------------------
// Clima: el ventilador se enciende al llegar a TEMP_UMBRAL_C y se apaga al
// bajar de (TEMP_UMBRAL_C - TEMP_HISTERESIS_C). Se puede cambiar en caliente
// con POST /config {"temp_umbral": 30}.
constexpr float TEMP_UMBRAL_C = 28.0f;
constexpr float TEMP_HISTERESIS_C = 2.0f;
constexpr float TEMP_UMBRAL_MIN_C = 15.0f;   // límites aceptados por POST /config
constexpr float TEMP_UMBRAL_MAX_C = 45.0f;

// Iluminación automática: es de noche cuando la luz (0..4095, mayor = más luz)
// baja de LDR_UMBRAL_NOCHE; deja de serlo al superar umbral + histéresis.
constexpr int LDR_UMBRAL_NOCHE = 1200;
constexpr int LDR_HISTERESIS = 150;
constexpr uint32_t LUZ_AUTO_ESPERA_MS = 30000;   // apagado tras este tiempo sin movimiento

// Modo seguridad
constexpr uint32_t PIR_CALENTAMIENTO_MS = 30000;     // el PIR da falsos positivos al encender
constexpr uint32_t EVENTO_ENFRIAMIENTO_MS = 3000;    // mínimo entre dos eventos del mismo tipo
constexpr uint32_t ALARMA_BUZZER_MAX_MS = 60000;     // el buzzer calla tras esto (la alarma sigue activa)
constexpr uint8_t MAX_EVENTOS = 10;                  // eventos que se guardan para la PC

// ---------------------------------------------------------------------------
// Tiempos (todo se hace con millis(), sin delay())
// ---------------------------------------------------------------------------
constexpr uint32_t LDR_INTERVALO_MS = 100;
constexpr uint32_t DHT_INTERVALO_MS = 2000;      // el DHT22 no admite lecturas más rápidas
constexpr uint8_t DHT_FALLOS_MAX = 5;            // lecturas fallidas seguidas antes de informar null
constexpr uint32_t OLED_INTERVALO_MS = 1000;
constexpr uint32_t WIFI_REINTENTO_MS = 10000;
constexpr uint32_t PANTALLA_IP_MS = 4000;        // cuánto se muestra la IP grande al arrancar
constexpr uint32_t ANTIRREBOTE_MS = 40;
constexpr uint16_t PITIDO_BOTON_MS = 60;
