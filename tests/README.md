# tests

Ejecutar desde la raíz del proyecto (no necesitan hardware):

```bash
python -m unittest discover -s tests -v
```

| Archivo | Qué prueba | Requiere |
|---|---|---|
| `test_nlu.py` | Catálogo de canciones, analizador por reglas (luces, ventilador, guardián, música, volumen, consultas, aclaraciones), modelo de respaldo con confirmación (Ollama simulado) y ejecutor (placa simulada). | numpy no; pyserial opcional |
| `test_dialogo.py` | El asistente de punta a punta: aclaraciones, cancelación, timeouts, errores de placa/modelo/micrófono, alertas de seguridad y registro de tiempos. | — |
| `test_serial_client.py` | Protocolo USB, eventos de la placa, desconexión y reconexión, líneas partidas, monitor. | — |
| `test_gui.py` | La interfaz sin pantalla (offscreen): clics, chat, alarma y desconexión. | PySide6 |
| `test_stt.py` | Detección de voz por energía, remuestreo, filtro de alucinaciones y errores del micrófono. | numpy |
| `test_tts.py` | Elección de voz en español, reintentos y modo texto. | — |

La placa simulada que usan los tests está en `pc/simulador_casa.py` y también sirve para
`python gui_app.py --demo`.
