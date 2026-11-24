"""
Pruebas del vocabulario nuevo: catálogo de canciones, analizador por reglas,
modelo de respaldo (Ollama simulado) y ejecutor (placa simulada).

    python -m unittest tests.test_nlu -v
"""

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))

import acciones  # noqa: E402
import entendedor as ent  # noqa: E402
import nlu  # noqa: E402
import serial_client as sc  # noqa: E402
from catalogo import Cancion, Catalogo, ErrorCatalogo  # noqa: E402
from config import SERIE, ZONAS_CASA  # noqa: E402
from nlu import Comando  # noqa: E402
from simulador_casa import CasaSimulada  # noqa: E402

TODAS = tuple(ZONAS_CASA)

CATALOGO = Catalogo([
    Cancion(2, "Thriller", ("la de michael jackson",)),
    Cancion(3, "Bohemian Rhapsody", ("la de queen",)),
    Cancion(4, "Despacito"),
    Cancion(5, "La Bamba"),
    Cancion(6, "Canción de cuna"),
])


def cmd(frase, pendiente=None, catalogo=CATALOGO):
    return nlu.analizar(frase, catalogo, pendiente)


def luces(accion, *zonas):
    return Comando(accion, "luces", tuple(zonas))


class TestCatalogo(unittest.TestCase):

    def test_busqueda(self):
        casos = {
            "pon thriller": 2, "quiero oir Thriller de Michael": 2, "la de queen": 3,
            "pon bohemian": 3, "ponme despacito por favor": 4, "la bamba": 5,
            "pon trailer": 2,          # error típico de transcripción
        }
        for frase, pista in casos.items():
            with self.subTest(frase=frase):
                c = CATALOGO.buscar(frase)
                self.assertIsNotNone(c, frase)
                self.assertEqual(c.pista, pista)

    def test_sin_coincidencia(self):
        for frase in ("prende la sala", "pon música", "apaga el ventilador", ""):
            with self.subTest(frase=frase):
                self.assertIsNone(CATALOGO.buscar(frase))

    def test_empate_no_adivina(self):
        c = Catalogo([Cancion(2, "Luna llena"), Cancion(3, "Luna nueva")])
        self.assertIsNone(c.buscar("pon la luna"))
        self.assertEqual(c.buscar("pon luna llena").pista, 2)

    def test_por_numero_y_voz(self):
        self.assertEqual(CATALOGO.por_numero(1).titulo, "Thriller")
        self.assertIsNone(CATALOGO.por_numero(0))
        self.assertIsNone(CATALOGO.por_numero(6))
        self.assertEqual(CATALOGO.titulos_para_voz(3), "Thriller, Bohemian Rhapsody, Despacito y 2 más")

    def test_validacion(self):
        with self.assertRaises(ErrorCatalogo):
            Catalogo([Cancion(1, "Alarma")])               # la 1 es la alarma
        with self.assertRaises(ErrorCatalogo):
            Catalogo([Cancion(2, "A"), Cancion(2, "B")])   # repetida
        with self.assertRaises(ErrorCatalogo):
            Catalogo([Cancion(2, "  ")])

    def test_cargar_json(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "c.json"
            ruta.write_text(json.dumps({"canciones": [{"pista": 7, "titulo": "Hola", "alias": ["saludo"]}]}),
                            encoding="utf-8")
            c = Catalogo.cargar(ruta)
            self.assertEqual(c.buscar("pon el saludo").pista, 7)
            ruta.write_text("{roto", encoding="utf-8")
            with self.assertRaises(ErrorCatalogo):
                Catalogo.cargar(ruta)
            self.assertEqual(len(Catalogo.cargar(Path(d) / "no_existe.json")), 0)

    def test_archivo_de_ejemplo_es_valido(self):
        self.assertEqual(len(Catalogo.cargar()), 10)


class TestLuces(unittest.TestCase):

    def test_comandos_completos(self):
        casos = {
            "prende la luz de la sala": luces("encender", "sala"),
            "apaga las luces de la cocina": luces("apagar", "cocina"),
            "enciende el foco del baño": luces("encender", "bano"),
            "apaga el cuarto": luces("apagar", "cuarto"),
            "prende la sala": luces("encender", "sala"),
            "enciende la luz del comedor": luces("encender", "comedor"),
            "prende las luces del patio": luces("encender", "exterior"),
            "apaga la luz de la entrada": luces("apagar", "exterior"),
            "enciende la luz del jardín": luces("encender", "exterior"),
            "prende la luz de la recámara": luces("encender", "cuarto"),
            "¡Apaga la luz del baño!": luces("apagar", "bano"),
            "PRENDE LA SALA": luces("encender", "sala"),
        }
        for frase, esperado in casos.items():
            with self.subTest(frase=frase):
                self.assertEqual(cmd(frase), esperado)

    def test_varias_zonas(self):
        self.assertEqual(cmd("prende la sala y el cuarto"), luces("encender", "sala", "cuarto"))
        self.assertEqual(cmd("apaga las luces de la cocina y el comedor"), luces("apagar", "cocina", "comedor"))

    def test_todas(self):
        self.assertEqual(cmd("apaga todas las luces"), luces("apagar", *TODAS))
        self.assertEqual(cmd("enciende todas las luces de la casa"), luces("encender", *TODAS))
        self.assertEqual(cmd("prende todos los focos"), luces("encender", *TODAS))

    def test_sin_zona_pregunta(self):
        for frase, verbo in (("prende las luces", "encender"), ("apaga la luz", "apagar"), ("enciende un foco", "encender")):
            with self.subTest(frase=frase):
                c = cmd(frase)
                self.assertTrue(c.es_ambiguo)
                self.assertEqual((c.accion, c.objetivo, c.zonas), (verbo, "luces", ()))
                self.assertIn(f"¿Qué luces quieres {verbo}", c.pregunta_aclaracion)
                self.assertIn("baño", c.pregunta_aclaracion)

    def test_sin_verbo_pregunta(self):
        c = cmd("las luces de la sala")
        self.assertTrue(c.es_ambiguo)
        self.assertIn("encender o apagar", c.pregunta_aclaracion)

    def test_iluminar_es_encender(self):
        self.assertEqual(cmd("ilumina el baño"), luces("encender", "bano"))
        self.assertEqual(cmd("alúmbrame la cocina"), luces("encender", "cocina"))

    def test_titulo_de_cancion_no_se_confunde_con_otras_palabras(self):
        c = Catalogo([Cancion(2, "Dos"), Cancion(3, "Sala de espera")])
        self.assertEqual(nlu.analizar("prende dos luces en la cocina", c), luces("encender", "cocina"))
        self.assertEqual(nlu.analizar("pon dos", c), Comando("reproducir", "musica", cancion=2))

    def test_para_no_es_un_verbo(self):
        self.assertEqual(cmd("quiero luz para la cocina").accion, "desconocido")
        c = cmd("enciende una luz para la cocina")
        self.assertEqual(c, luces("encender", "cocina"))


class TestVentiladorGuardianTodo(unittest.TestCase):

    def test_ventilador(self):
        self.assertEqual(cmd("enciende el ventilador"), Comando("encender", "ventilador"))
        self.assertEqual(cmd("apaga el ventilador"), Comando("apagar", "ventilador"))
        self.assertEqual(cmd("pon el ventilador"), Comando("encender", "ventilador"))
        self.assertEqual(cmd("apaga el abanico de la sala"), Comando("apagar", "ventilador"))   # zona ignorada
        self.assertTrue(cmd("el ventilador").es_ambiguo)

    def test_guardian_y_alarma(self):
        self.assertEqual(cmd("activa el modo guardián"), Comando("encender", "guardian"))
        self.assertEqual(cmd("activa la alarma"), Comando("encender", "guardian"))
        self.assertEqual(cmd("activa el modo seguridad"), Comando("encender", "guardian"))
        self.assertEqual(cmd("modo guardián"), Comando("encender", "guardian"))
        self.assertEqual(cmd("desactiva el guardián"), Comando("apagar", "guardian"))
        for frase in ("apaga la alarma", "silencia la alarma", "desactiva la alarma", "calla la sirena"):
            with self.subTest(frase=frase):
                self.assertEqual(cmd(frase), Comando("apagar", "alarma"))

    def test_todo(self):
        self.assertEqual(cmd("apaga todo"), Comando("apagar", "todo"))
        self.assertEqual(cmd("enciende todo"), Comando("encender", "todo"))
        self.assertIsNone(cmd("todo"))

    def test_varios_objetivos_pide_una_orden_a_la_vez(self):
        for frase in ("prende la sala y el ventilador", "enciende el ventilador y las luces de la cocina"):
            with self.subTest(frase=frase):
                c = cmd(frase)
                self.assertEqual(c.accion, "desconocido")
                self.assertIn("a la vez", c.nota)

    def test_zona_junto_al_aparato_se_ignora(self):
        # Sin "y": la zona califica al aparato, no es otra orden.
        self.assertEqual(cmd("apaga el ventilador de la sala"), Comando("apagar", "ventilador"))


class TestMusica(unittest.TestCase):

    def test_por_titulo_y_alias(self):
        casos = {"pon thriller": 2, "reproduce bohemian rhapsody": 3, "pon la de queen": 3,
                 "quiero escuchar despacito": 4, "ponme la bamba": 5, "toca despacito": 4}
        for frase, pista in casos.items():
            with self.subTest(frase=frase):
                self.assertEqual(cmd(frase), Comando("reproducir", "musica", cancion=pista))

    def test_por_numero(self):
        casos = {"pon la canción 1": 2, "reproduce la canción tres": 4, "pon la canción número 5": 6,
                 "pon la pista dos": 3}
        for frase, pista in casos.items():
            with self.subTest(frase=frase):
                self.assertEqual(cmd(frase), Comando("reproducir", "musica", cancion=pista))

    def test_numero_fuera_de_rango(self):
        c = cmd("pon la canción 9")
        self.assertTrue(c.es_ambiguo)
        self.assertIn("Solo tengo 5", c.pregunta_aclaracion)

    def test_sin_cancion_pregunta(self):
        c = cmd("pon música")
        self.assertTrue(c.es_ambiguo)
        self.assertIn("¿Qué canción", c.pregunta_aclaracion)
        self.assertIn("Thriller", c.pregunta_aclaracion)

    def test_aleatoria(self):
        for frase in ("pon cualquier canción", "pon algo de música", "sorpréndeme con una canción"):
            with self.subTest(frase=frase):
                self.assertEqual(cmd(frase), Comando("reproducir", "musica", aleatoria=True))

    def test_detener(self):
        for frase in ("para la música", "detén la música", "apaga la música", "pausa la canción", "quita la música"):
            with self.subTest(frase=frase):
                self.assertEqual(cmd(frase), Comando("detener", "musica"))

    def test_volumen(self):
        self.assertEqual(cmd("sube el volumen"), Comando("subir", "volumen"))
        self.assertEqual(cmd("baja el volumen"), Comando("bajar", "volumen"))
        self.assertEqual(cmd("más fuerte"), Comando("subir", "volumen"))
        self.assertEqual(cmd("baja la música"), Comando("bajar", "volumen"))
        self.assertEqual(cmd("sube la música"), Comando("subir", "volumen"))
        self.assertEqual(cmd("pon el volumen al 50"), Comando("fijar", "volumen", valor=15))
        self.assertEqual(cmd("volumen al máximo"), Comando("fijar", "volumen", valor=30))
        self.assertEqual(cmd("volumen al 100"), Comando("fijar", "volumen", valor=30))
        self.assertEqual(cmd("volumen al mínimo"), Comando("fijar", "volumen", valor=0))

    def test_musica_fuerte_no_es_subir_volumen(self):
        self.assertEqual(cmd("pon thriller fuerte"), Comando("reproducir", "musica", cancion=2))


class TestConsultas(unittest.TestCase):

    def test_consultas(self):
        casos = {"qué temperatura hace": "temperatura", "cuántos grados hay": "temperatura",
                 "dime la humedad": "humedad", "cómo está la casa": "estado", "cuál es la temperatura": "temperatura"}
        for frase, objetivo in casos.items():
            with self.subTest(frase=frase):
                self.assertEqual(cmd(frase), Comando("consultar", objetivo))


class TestNoReconocido(unittest.TestCase):

    def test_pasa_al_llm(self):
        for frase in ("cuéntame un chiste", "qué hora es", "déjame ver en el baño", "hace mucho calor", "hola", ""):
            with self.subTest(frase=frase):
                self.assertIsNone(cmd(frase))


class TestAclaraciones(unittest.TestCase):

    def test_zona(self):
        p = cmd("prende las luces")
        self.assertEqual(cmd("en la sala", p), luces("encender", "sala"))
        self.assertEqual(cmd("la cocina y el baño", p), luces("encender", "bano", "cocina"))
        self.assertEqual(cmd("todas", p), luces("encender", *TODAS))
        self.assertEqual(cmd("la del jardín", p), luces("encender", "exterior"))

    def test_cambio_de_accion_y_de_idea(self):
        p = cmd("prende las luces")
        self.assertEqual(cmd("mejor apágalas en la sala", p), luces("apagar", "sala"))
        self.assertEqual(cmd("no, mejor el ventilador", p), Comando("encender", "ventilador"))

    def test_respuesta_inutil_no_cambia_nada(self):
        p = cmd("prende las luces")
        self.assertEqual(cmd("no sé", p), p)
        self.assertEqual(cmd("mmm", p), p)

    def test_zona_inexistente_sigue_pendiente(self):
        p = cmd("prende las luces")
        self.assertEqual(cmd("la azotea", p), p)

    def test_falta_la_accion(self):
        p = cmd("las luces de la sala")
        self.assertEqual(cmd("apágalas", p), luces("apagar", "sala"))
        self.assertEqual(cmd("enciéndelas", p), luces("encender", "sala"))

    def test_falta_el_objetivo_del_ventilador(self):
        p = cmd("el ventilador")
        self.assertEqual(cmd("apágalo", p), Comando("apagar", "ventilador"))

    def test_cancion(self):
        p = cmd("pon música")
        self.assertEqual(cmd("la tres", p), Comando("reproducir", "musica", cancion=4))
        self.assertEqual(cmd("la primera", p), Comando("reproducir", "musica", cancion=2))
        self.assertEqual(cmd("thriller", p), Comando("reproducir", "musica", cancion=2))
        self.assertEqual(cmd("cualquiera", p), Comando("reproducir", "musica", aleatoria=True))
        self.assertEqual(cmd("la de queen", p), Comando("reproducir", "musica", cancion=3))

    def test_cancion_fuera_de_rango(self):
        p = cmd("pon música")
        r = cmd("la ocho", p)
        self.assertTrue(r.es_ambiguo)
        self.assertIn("Solo tengo 5", r.pregunta_aclaracion)

    def test_volumen(self):
        p = cmd("pon el volumen")
        self.assertTrue(p.es_ambiguo)
        self.assertEqual(cmd("al 40", p), Comando("fijar", "volumen", valor=12))
        self.assertEqual(cmd("al máximo", p), Comando("fijar", "volumen", valor=30))

    def test_orden_nueva_en_medio_de_una_aclaracion(self):
        p = cmd("prende las luces")
        self.assertEqual(cmd("apaga el ventilador", p), Comando("apagar", "ventilador"))
        self.assertEqual(cmd("pon thriller", p), Comando("reproducir", "musica", cancion=2))


# ---------------------------------------------------------------------------
# Modelo de respaldo
# ---------------------------------------------------------------------------

class OllamaFalso:
    def __init__(self, *respuestas, falla=False):
        self.respuestas = list(respuestas)
        self.falla = falla
        self.llamadas = []

    def chat(self, **kwargs):
        self.llamadas.append(kwargs)
        if self.falla:
            raise ConnectionError("Ollama caído")
        return {"message": {"content": self.respuestas.pop(0)}}

    def list(self):
        return {"models": [{"model": ent.LLM.modelo}]}

    def generate(self, **kwargs):
        return {}


def salida(accion, objetivo="ninguno", zonas=(), cancion=None, valor=None):
    return json.dumps({"accion": accion, "objetivo": objetivo, "zonas": list(zonas),
                       "cancion": cancion, "valor": valor})


class TestEntendedor(unittest.TestCase):

    def test_las_reglas_no_llaman_al_modelo(self):
        ollama = OllamaFalso()
        e = ent.Entendedor(CATALOGO, ollama)
        self.assertEqual(e.interpretar("prende la sala"), luces("encender", "sala"))
        self.assertEqual(e.ultimo_origen, "reglas")
        self.assertEqual(ollama.llamadas, [])

    def test_aclaracion_no_llama_al_modelo(self):
        ollama = OllamaFalso()
        e = ent.Entendedor(CATALOGO, ollama)
        p = e.interpretar("prende las luces")
        self.assertEqual(e.interpretar("no sé", p), p)
        self.assertEqual(ollama.llamadas, [])

    def test_parafrasis_con_el_modelo(self):
        ollama = OllamaFalso(salida("encender", "luces", ["bano"]))
        e = ent.Entendedor(CATALOGO, ollama)
        c = e.interpretar("déjame ver en el baño")
        self.assertEqual(e.ultimo_origen, "llm")
        # Lo que supone el modelo se confirma antes de ejecutarse.
        self.assertTrue(c.es_ambiguo)
        self.assertEqual(c.pregunta_aclaracion, "¿Quieres que encienda las luces del baño?")
        self.assertEqual(c.propuesta, luces("encender", "bano"))
        self.assertEqual(nlu.analizar("sí", CATALOGO, c), luces("encender", "bano"))
        llamada = ollama.llamadas[0]
        self.assertEqual(llamada["format"], ent.ESQUEMA)         # salida restringida por esquema
        self.assertLessEqual(llamada["options"]["temperature"], 0.3)

    def test_zona_inventada_se_descarta(self):
        ollama = OllamaFalso(salida("encender", "luces", ["sala"]))   # el usuario no dijo ninguna zona
        c = ent.Entendedor(CATALOGO, ollama).interpretar("ilumina un poco")
        self.assertTrue(c.es_ambiguo)
        self.assertEqual(c.zonas, ())

    def test_ventilador_por_parafrasis(self):
        ollama = OllamaFalso(salida("encender", "ventilador"))
        c = ent.Entendedor(CATALOGO, ollama).interpretar("refréscame un poco")
        self.assertEqual(c.pregunta_aclaracion, "¿Quieres que encienda el ventilador?")
        self.assertEqual(c.propuesta, Comando("encender", "ventilador"))

    def test_combinaciones_absurdas_no_ejecutan_nada(self):
        casos = [
            (salida("reproducir", "ventilador"), "refréscame un poco"),      # pregunta qué hacer
            (salida("consultar", "luces", ["cocina"]), "no veo nada en la cocina"),
        ]
        for crudo, frase in casos:
            with self.subTest(frase=frase):
                c = ent.Entendedor(CATALOGO, OllamaFalso(crudo)).interpretar(frase)
                self.assertTrue(c.es_ambiguo)
                self.assertIn("encender o apagar", c.pregunta_aclaracion)
        # Con objetivos que actuarían sin preguntar, se descarta todo.
        for crudo in (salida("consultar", "guardian"), salida("reproducir", "alarma"), salida("encender", "volumen")):
            with self.subTest(crudo=crudo):
                c = ent.Entendedor(CATALOGO, OllamaFalso(crudo)).interpretar("haz algo")
                self.assertEqual(c, Comando.desconocido())

    def test_lo_que_supone_el_modelo_nunca_se_ejecuta_sin_confirmar(self):
        # "cierra la puerta" -> el modelo dice "apagar guardián": peligroso si se ejecutara directo.
        c = ent.Entendedor(CATALOGO, OllamaFalso(salida("apagar", "guardian"))).interpretar("cierra la puerta")
        self.assertFalse(c.ejecutable)
        self.assertEqual(c.pregunta_aclaracion, "¿Quieres que desactive el modo guardián?")
        self.assertEqual(nlu.analizar("no", CATALOGO, c).nota, "De acuerdo, no hago nada.")
        self.assertEqual(nlu.analizar("sí", CATALOGO, c), Comando("apagar", "guardian"))

    def test_confirmacion_con_otra_orden_o_sin_sentido(self):
        c = ent.Entendedor(CATALOGO, OllamaFalso(salida("encender", "ventilador"))).interpretar("refréscame")
        self.assertEqual(nlu.analizar("mejor prende la sala", CATALOGO, c), luces("encender", "sala"))
        self.assertEqual(nlu.analizar("mmm", CATALOGO, c), c)            # sin entender: repite la pregunta
        self.assertEqual(nlu.analizar("sí, por favor", CATALOGO, c), Comando("encender", "ventilador"))
        self.assertEqual(nlu.analizar("no, gracias", CATALOGO, c).accion, "desconocido")

    def test_consulta_del_modelo_no_pide_confirmacion(self):
        c = ent.Entendedor(CATALOGO, OllamaFalso(salida("consultar", "temperatura"))).interpretar("cuántos grados hay aquí dentro")
        self.assertEqual(c, Comando("consultar", "temperatura"))

    def test_consulta_exige_palabra_de_consulta(self):
        c = ent.Entendedor(CATALOGO, OllamaFalso(salida("consultar", "estado"))).interpretar("qué hora es")
        self.assertEqual(c, Comando.desconocido())

    def test_fuera_de_dominio(self):
        ollama = OllamaFalso(salida("desconocido", "ninguno"))
        self.assertEqual(ent.Entendedor(CATALOGO, ollama).interpretar("cuéntame un chiste"), Comando.desconocido())

    def test_json_inventado_se_ignora(self):
        for crudo in ("basura", "[1,2]", json.dumps({"accion": "volar", "objetivo": "luna", "zonas": []})):
            with self.subTest(crudo=crudo):
                c = ent.Entendedor(CATALOGO, OllamaFalso(crudo)).interpretar("haz algo raro")
                self.assertEqual(c, Comando.desconocido())

    def test_volumen_del_modelo_solo_si_hay_numero(self):
        e = ent.Entendedor(CATALOGO, OllamaFalso(salida("fijar", "volumen", valor=25), salida("fijar", "volumen", valor=25)))
        sin_numero = e.interpretar("ajusta el sonido a lo que sea")
        self.assertTrue(sin_numero.es_ambiguo)
        con_numero = e.interpretar("ajusta el sonido a 25")
        self.assertEqual(con_numero.propuesta, Comando("fijar", "volumen", valor=25))
        self.assertEqual(con_numero.pregunta_aclaracion, "¿Quieres que ponga el volumen al 83 por ciento?")

    def test_sin_ollama_funcionan_las_reglas(self):
        e = ent.Entendedor(CATALOGO, OllamaFalso(falla=True))
        self.assertEqual(e.interpretar("apaga el ventilador"), Comando("apagar", "ventilador"))
        self.assertEqual(e.interpretar("déjame ver en el baño"), Comando.desconocido())   # el modelo falla: degrada
        sin_cliente = ent.Entendedor(CATALOGO, usar_llm=False)
        sin_cliente._cliente = None
        sin_cliente.llm_disponible = False
        self.assertEqual(sin_cliente.interpretar("déjame ver en el baño"), Comando.desconocido())

    def test_verificar(self):
        self.assertTrue(ent.Entendedor(CATALOGO, OllamaFalso()).verificar())

        class SinModelo(OllamaFalso):
            def list(self):
                return {"models": [{"model": "otro:1b"}]}

        e = ent.Entendedor(CATALOGO, SinModelo())
        self.assertFalse(e.verificar())
        self.assertFalse(e.llm_disponible)

    def test_texto_vacio(self):
        e = ent.Entendedor(CATALOGO, OllamaFalso())
        self.assertEqual(e.interpretar("   "), Comando.desconocido())
        p = e.interpretar("prende las luces")
        self.assertEqual(e.interpretar("", p), p)


# ---------------------------------------------------------------------------
# Ejecutor
# ---------------------------------------------------------------------------

CFG = dataclasses.replace(SERIE, puerto="auto", timeout_lectura_s=0.02, timeout_respuesta_s=0.3, espera_arranque_s=0.6)


class TestEjecutor(unittest.TestCase):

    def setUp(self):
        self.placa = CasaSimulada()
        self.cliente = sc.ClienteSerie(CFG, fabrica_puerto=self.placa.abrir)
        self.cliente.conectar()
        self.addCleanup(self.cliente.cerrar)
        self.ejecutor = acciones.Ejecutor(self.cliente, CATALOGO)

    def ejecutar(self, frase, pendiente=None):
        c = nlu.analizar(frase, CATALOGO, pendiente)
        return self.ejecutor.ejecutar(c).mensaje

    def test_luces(self):
        self.assertEqual(self.ejecutar("prende la sala"), "Luces de la sala encendidas.")
        self.assertTrue(self.placa.leds[3])                       # la sala es el foco 4
        self.assertEqual(self.ejecutar("apaga la luz del baño"), "Luces del baño apagadas.")
        self.assertEqual(self.ejecutar("prende la cocina y el cuarto"), "Luces de la cocina y el cuarto encendidas.")
        self.assertEqual(self.placa.leds, [False, True, False, True, True, False])   # la sala seguía encendida
        self.assertEqual(self.ejecutar("enciende todas las luces"), "Todas las luces encendidas.")
        self.assertTrue(all(self.placa.leds))
        self.assertEqual(self.ejecutar("prende la luz del jardín"), "Luces del exterior encendidas.")

    def test_ventilador_guardian_alarma(self):
        self.assertEqual(self.ejecutar("enciende el ventilador"), "Ventilador encendido.")
        self.assertTrue(self.placa.ventilador)
        self.assertEqual(self.ejecutar("activa el modo guardián"), "Modo guardián activado.")
        self.assertTrue(self.placa.guardian)
        self.placa.disparar_intruso()
        self.assertEqual(self.ejecutar("apaga la alarma"), "Alarma apagada y modo guardián desactivado.")
        self.assertEqual((self.placa.alarma, self.placa.guardian), (False, False))

    def test_todo(self):
        self.ejecutar("enciende todo")
        self.assertTrue(all(self.placa.leds) and self.placa.ventilador)
        self.ejecutar("pon thriller")
        self.assertEqual(self.ejecutar("apaga todo"), "Todo apagado.")
        self.assertFalse(any(self.placa.leds) or self.placa.ventilador)
        self.assertEqual(self.placa.pista, 0)                     # también paró la música

    def test_musica(self):
        self.assertEqual(self.ejecutar("pon thriller"), "Reproduciendo Thriller.")
        self.assertEqual(self.placa.pista, 2)
        self.assertEqual(self.ejecutar("pon la canción tres"), "Reproduciendo Despacito.")
        self.assertEqual(self.ejecutar("para la música"), "Música detenida.")
        self.assertEqual(self.placa.pista, 0)

    def test_aleatoria_no_repite_seguida(self):
        vistas = []
        for _ in range(8):
            self.ejecutar("pon cualquier canción")
            vistas.append(self.placa.pista)
        self.assertTrue(all(a != b for a, b in zip(vistas, vistas[1:])))
        self.assertTrue(all(2 <= p <= 6 for p in vistas))

    def test_musica_con_alarma_sonando(self):
        self.ejecutar("activa la alarma")
        self.placa.disparar_intruso()
        self.assertEqual(self.ejecutar("pon thriller"), "La alarma está sonando. Apágala primero.")

    def test_sin_reproductor(self):
        placa = CasaSimulada(con_mp3=False)
        with sc.ClienteSerie(CFG, fabrica_puerto=placa.abrir) as c:
            msg = acciones.Ejecutor(c, CATALOGO).ejecutar(nlu.analizar("pon thriller", CATALOGO)).mensaje
        self.assertIn("reproductor", msg)

    def test_volumen(self):
        self.assertEqual(self.placa.volumen, 14)
        self.assertEqual(self.ejecutar("sube el volumen"), "Volumen al 60 por ciento.")   # 14 + 4 = 18 de 30
        self.assertEqual(self.placa.volumen, 18)
        self.ejecutar("baja el volumen")
        self.assertEqual(self.placa.volumen, 14)
        self.assertEqual(self.ejecutar("volumen al máximo"), "Volumen al 100 por ciento.")
        self.assertEqual(self.ejecutar("sube el volumen"), "El volumen ya está al máximo.")
        self.ejecutar("volumen al mínimo")
        self.assertEqual(self.ejecutar("baja el volumen"), "El volumen ya está al mínimo.")

    def test_consultas(self):
        self.assertEqual(self.ejecutar("qué temperatura hace"), "La temperatura es de 24,5 grados.")
        self.assertEqual(self.ejecutar("dime la humedad"), "La humedad es del 58 por ciento.")
        self.placa.leds[3] = self.placa.leds[4] = True
        resumen = self.ejecutar("cómo está la casa")
        self.assertIn("24,5 grados", resumen)
        self.assertIn("Luces encendidas: la sala y el cuarto.", resumen)
        self.assertIn("guardián está desactivado", resumen)
        self.placa.temperatura = None
        self.assertIn("No puedo leer", self.ejecutar("qué temperatura hace"))

    def test_placa_desconectada_propaga(self):
        self.placa.desenchufar()
        self.placa.silenciosa = True
        with self.assertRaises(sc.ErrorConexionCasa):
            self.ejecutar("prende la sala")

    def test_no_ejecuta_ambiguos(self):
        with self.assertRaises(ValueError):
            self.ejecutor.ejecutar(nlu.analizar("prende las luces", CATALOGO))


if __name__ == "__main__":
    unittest.main()
