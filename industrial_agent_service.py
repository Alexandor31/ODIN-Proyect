"""
industrial_agent_service.py
============================
Clase: IndustrialAgentService

Integra la lógica de IA para responder consultas del operador.
Soporta dos modos:
  * Modo "local" (por defecto): regex + reglas + lectura real del PLC.
    No requiere clave de API; ideal para entornos industriales on-prem.
  * Modo "openai" / "anthropic": usando la API del proveedor externo.
    Descomentar la librería correspondiente en requirements.txt.

El objetivo es proporcionar una respuesta textual coherente, lista para
ser sintetizada por el TTS del navegador.

IMPORTANTE: las escrituras al PLC requieren SIEMPRE confirmación explícita
por voz/texto del operador (ver `process_query`). El estado de la acción
pendiente vive en memoria en `self._pending_write`; es single-instance y
single-operator por diseño. Si en el futuro hay varias sesiones, mover el
pendiente a un store compartido (Redis) o usar session_id por operador.
"""

import os
import re
import time
import logging
import asyncio
from typing import Dict, Any, Optional

from ai_context_manager import AIContextManager
from plc_manager import PLCManager

logger = logging.getLogger("DANTEIndustrial.IndustrialAgentService")

# ---------------------------------------------------------------------- #
#  Constantes del flujo de confirmación previa a la escritura            #
# ---------------------------------------------------------------------- #
_CONFIRM_TIMEOUT_S = 45.0

# Tope de espera para el proveedor LLM remoto. Pasado este tiempo se
# responde en modo local: en planta es preferible una respuesta
# aproximada inmediata a la correcta que nunca llega.
_LLM_TIMEOUT_S = 6.0

_CONFIRM_WORDS = {
    "si", "sí", "confirma", "confirmar", "confirmado",
    "aplica", "aplicalo", "aplicar",
    "hazlo", "dale", "ok", "vale", "adelante", "procede",
}

_CANCEL_WORDS = {
    "no", "cancela", "cancelar", "anula", "anular",
    "olvidalo", "nada",
}


class IndustrialAgentService:
    """
    Servicio que responde preguntas sobre el estado de la planta.
    Combina el contexto de configuración con la lectura real del PLC.
    """

    def __init__(
        self,
        context: AIContextManager,
        plc_managers: Dict[str, PLCManager],
    ):
        self.context = context
        self.plc_managers = plc_managers
        self.config_global = context.get_global_config()
        self.provider = self.config_global.get("ia_provider", "local")
        self.model = self.config_global.get("ia_model", "DANTE-engine-v1")

        # Diccionario de sinónimos /alias en español para mapear nombres coloquiales
        # a nombres reales de variables en el PLC (derivados de config_plcs.json).
        # Si se añade una variable nueva al JSON, basta con añadir aquí su alias.
        self.aliases: Dict[str, list] = {
            # presión
            "presion":   ["presion_bomba"],
            "presión":   ["presion_bomba"],
            "bar":       ["presion_bomba"],
            "psi":       ["presion_bomba"],
            # velocidad (lectura; el setpoint se detecta por intent de acción)
            "velocidad": ["velocidad_motor"],
            "rpm":       ["velocidad_motor"],
            "porcentaje":["velocidad_motor"],
            # corriente
            "corriente": ["corriente_motor"],
            "amperios":  ["corriente_motor"],
            "amperaje":  ["corriente_motor"],
            # energía / voltaje
            "energia":   ["enegia_motor"],
            "energía":   ["enegia_motor"],
            "voltios":   ["enegia_motor"],
            "voltaje":   ["enegia_motor"],
            "votios":    ["enegia_motor"],
            # tanques (Tanque 1 y Tanque 2 son PLCs separados en el JSON;
            # "nivel_tanque3" ya no existe)
            "nivel":     ["nivel_tanque1", "nivel_tanque2"],
            "tanque":    ["nivel_tanque1", "nivel_tanque2"],
            "tanque1":   ["nivel_tanque1"],
            "tanque2":   ["nivel_tanque2"],
            "t1":        ["nivel_tanque1"],
            "t2":        ["nivel_tanque2"],
            # también el nombre del PLC completo como alias
            "tanque 1":  ["nivel_tanque1"],
            "tanque 2":  ["nivel_tanque2"],
            "tanques":   ["nivel_tanque1", "nivel_tanque2"],
            # estado del motor (coil lectura) y variables asociadas.
            # NOTA: el alias "estado" se quitó porque era demasiado genérico
            # ("estado del tanque 1", "estado del IED") lo activaban y ensuciaba
            # la respuesta. Si el operador dice solo "estado del motor", el
            # alias "motor" sigue cubriendo las variables del motor.
            "bomba":     ["estado_bomba", "presion_bomba"],
            "motor":     ["velocidad_motor", "corriente_motor", "enegia_motor"],
            # IED — el alias "ied" debe ganar sobre cualquier "estado" genérico
            "ied":            ["estado_ied", "prot50_ied", "prot27_ied", "prot31_ied"],
            "rele":           ["estado_ied", "prot50_ied", "prot27_ied", "prot31_ied"],
            "relé":           ["estado_ied", "prot50_ied", "prot27_ied", "prot31_ied"],
            "proteccion":     ["prot50_ied", "prot27_ied", "prot31_ied"],
            "protección":     ["prot50_ied", "prot27_ied", "prot31_ied"],
            "50":             ["prot50_ied"],
            "27":             ["prot27_ied"],
            "31":             ["prot31_ied"],
            "sobrecorriente": ["prot50_ied"],
            "subtension":     ["prot27_ied"],
            "subtensión":     ["prot27_ied"],
            "excitacion":     ["prot31_ied"],
            "excitación":     ["prot31_ied"],
            # NOTA: "alarma"/"emergencia"/"alertas"/"problemas" se manejan
            # como intent keywords en _process_local (no como alias de variable).
        }

        # Acciones por voz: mapean palabra de intención a (variable_escribible, tipo)
        # Derivado automáticamente desde config_plcs.json -> registros.escribibles.
        # Si añades una variable escribible nueva al JSON, se detecta sola.
        self.action_aliases: Dict[str, Dict[str, Any]] = self._build_action_aliases()

        # Acción pendiente de confirmación. Solo una a la vez (single-operator).
        # Estructura: {cfg, valor, plc_id, created_at, tts_descripcion,
        #             kind, valor_legible, plc_nombre}.
        self._pending_write: Optional[Dict[str, Any]] = None

        logger.info(
            f"IndustrialAgentService inicializado (provider={self.provider}, model={self.model})"
        )

    # ------------------------------------------------------------------ #
    #  Punto de entrada principal
    # ------------------------------------------------------------------ #
    async def process_query(
        self, query: str, history: Optional[list] = None
    ) -> Dict[str, Any]:
        """
        Procesa una consulta de voz/texto y devuelve:
          {
            "respuesta":  "...",         # texto para TTS
            "datos":      {...},         # datos crudos leídos del PLC
            "timestamp":  "ISO-8601",
            "accion":     {...} | None   # si se ejecutó una acción
          }

        Flujo:
          1. Si hay una acción pendiente y el mensaje es "sí" → ejecuta.
          2. Si hay una acción pendiente y el mensaje es "no" → cancela.
          3. Si hay una acción pendiente y el mensaje es OTRA COSA → cancela
             el pendiente y procesa la nueva consulta.
          4. Si no hay pendiente y el mensaje es una orden de escritura →
             crea un pendiente (NO escribe) y pide confirmación por TTS.
          5. En cualquier otro caso, delega en `_process_query_core` (lectura).
        """
        history = history or []
        query_norm = (query or "").strip().lower()

        # 1) Limpiar pending por timeout
        if self._pending_write is not None:
            age = time.monotonic() - self._pending_write["created_at"]
            if age > _CONFIRM_TIMEOUT_S:
                logger.info(
                    f"pending_write cancelado por timeout ({age:.1f}s) "
                    f"para {self._pending_write.get('variable')}"
                )
                self._pending_write = None

        # 2) Clasificar contra pending activo
        if self._pending_write is not None:
            decision = _classify_confirmation(query_norm)
            if decision == "confirm":
                return await self._execute_pending_write()
            if decision == "cancel":
                self._pending_write = None
                return {
                    "respuesta": "Entendido, cancelado.",
                    "datos": {
                        "intent": "cancelacion",
                        "pendiente_descartado": True,
                    },
                    "timestamp": _now_iso(),
                }
            # Cualquier otra cosa: descarta pending y procesa la nueva consulta
            prefijo = "He descartado la confirmación anterior. "
            self._pending_write = None
            result = await self._process_query_core(query_norm, query)
            if isinstance(result, dict) and result.get("respuesta"):
                result["respuesta"] = prefijo + result["respuesta"]
            return result

        # 3) Sin pending: ¿es una nueva orden de escritura?
        try:
            pending = await self._detect_pending_action(query_norm)
        except Exception as e:
            logger.exception("Error detectando acción pendiente")
            pending = {
                "respuesta": (
                    "Disculpa, ocurrió un error al analizar tu orden. "
                    "Inténtalo de nuevo."
                ),
                "datos": {"error": str(e)},
            }

        if pending is None:
            return await self._process_query_core(query_norm, query)

        # Si el detector devolvió un dict con "respuesta" es un mensaje
        # informativo (rango inválido, falta número, etc.) — NO crear pending.
        if "respuesta" in pending and "cfg" not in pending:
            return pending

        # Es una acción válida → guardar y pedir confirmación
        self._pending_write = pending
        return {
            "respuesta": (
                f"Voy a escribir {pending['tts_descripcion']} "
                f"en {pending['plc_nombre']}. ¿Confirmas?"
            ),
            "datos": {
                "intent": "escritura_pendiente",
                "requiere_confirmacion": True,
                "variable": pending["variable"],
                "valor": pending["valor"],
                "plc_id": pending["plc_id"],
            },
            "timestamp": _now_iso(),
            "accion": None,
        }

    async def _process_query_core(
        self, query_norm: str, query_raw: str
    ) -> Dict[str, Any]:
        """Lectura/consulta normal (sin rama de acciones).

        Estrategia:
          1. Si `ia_provider=local`, usar siempre el modo local.
          2. Si la consulta es ESPECÍFICA (matchea una variable/alarma
             conocida), usar el modo local (rápido, determinista, lee
             PLC en vivo).
          3. Solo si la consulta es ABIERTA/AMBIGUA, escalar al LLM,
             y con tope de tiempo: si el LLM remoto se atasca, el
             operador no puede seguir operando.
          4. Si el LLM falla (timeout, red, token), degradar al modo
             local como respaldo.
        """
        # 1) Modo local puro: siempre local
        if self.provider == "local":
            return await self._process_local(query_norm, query_raw)

        # 2) ¿La consulta matchea algo específico del JSON?
        if self._can_handle_locally(query_norm):
            logger.debug("Consulta específica -> modo local directo")
            return await self._process_local(query_norm, query_raw)

        # 3) Consulta abierta: escalar al LLM con tope de tiempo
        try:
            if self.provider == "openai":
                coro = self._process_openai(query_raw)
            elif self.provider == "anthropic":
                coro = self._process_anthropic(query_raw)
            elif self.provider == "seius":
                coro = self._process_seius(query_raw)
            else:
                logger.warning(
                    f"Proveedor '{self.provider}' desconocido, fallback a local"
                )
                return await self._process_local(query_norm, query_raw)

            try:
                return await asyncio.wait_for(coro, timeout=_LLM_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(
                    f"LLM '{self.provider}' sin respuesta en {_LLM_TIMEOUT_S}s; "
                    f"se responde en modo local."
                )
                return await self._process_local(query_norm, query_raw)
        except Exception as e:
            logger.exception(f"Error en proveedor LLM '{self.provider}': {e}")
            # Degradar al modo local como respaldo
            return await self._process_local(query_norm, query_raw)

    def _can_handle_locally(self, query_norm: str) -> bool:
        """
        Devuelve True si la consulta puede resolverse con la lógica local
        (sin necesidad de un LLM). Considera:
          - Si menciona una variable/PLC conocido por alias o nombre.
          - Si pregunta explícitamente por alarmas/estado general.
        """
        # Si matchea alguna variable (alias o nombre técnico) -> local
        if self._extract_variables(query_norm):
            return True
        # Alarmas -> local
        if any(k in query_norm for k in ["alarma", "alertas", "problemas", "fallos"]):
            return True
        # Estado general explícito -> local
        general_kw = [
            "estado general", "resumen", "como va todo", "cómo va todo",
            "dame el estado", "estado de la planta", "reporte general",
            "status general", "situacion general", "situación general",
        ]
        if any(k in query_norm for k in general_kw):
            return True
        # Conteo de PLCs -> local
        if any(k in query_norm for k in ["cuantos plcs", "cuántos plcs",
                                          "lista de plcs", "qué plcs"]):
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Modo LOCAL (reglas + lectura real)
    # ------------------------------------------------------------------ #
    async def _process_local(self, query_norm: str, query_raw: str) -> Dict[str, Any]:
        """
        En modo local:
          - Identifica la intención (estado general, valor concreto, alarma, setpoint).
          - Si es un valor concreto, lee el PLC en vivo.
          - Si es una intención global, resume todos los PLCs.

        Orden de prioridad (importante):
          1. Si la consulta nombra una variable/PLC específico -> lectura puntual.
          2. Si menciona "alarmas" -> lista de alarmas activas.
          3. Si pregunta por "estado general" SIN nombrar nada concreto -> resumen.
          4. Si no se entiende -> fallback genérico.
        """

        # 1) Variables específicas (por alias coloquial o nombre técnico).
        #    Esto se hace PRIMERO porque "estado del tanque 1" menciona una
        #    variable concreta y debe responderse puntualmente, no como
        #    "estado general de la planta".
        variables = self._extract_variables(query_norm)
        if variables:
            return await self._answer_specific_variables(variables, query_raw)

        # 2) Intención: ¿qué alarmas hay activas?
        if any(k in query_norm for k in ["alarma", "alertas", "problemas", "fallos"]):
            return await self._answer_active_alarms(query_raw)

        # 3) Intención: estado general (solo si NO hay variable específica
        #    Y la consulta es claramente "dame todo"). Requiere palabras
        #    clave más fuertes para no atrapar consultas puntuales.
        general_kw = [
            "estado general", "resumen", "como va todo", "cómo va todo",
            "dame el estado", "estado de la planta", "reporte general",
            "status general", "situacion general", "situación general",
        ]
        if any(k in query_norm for k in general_kw):
            return await self._answer_general_status()

        # 4) ¿Cuántos PLCs?
        if any(k in query_norm for k in ["cuantos plcs", "cuántos plcs",
                                          "lista de plcs", "qué plcs"]):
            plc_ids = self.context.get_plc_ids()
            return {
                "respuesta": (
                    f"Hay {len(plc_ids)} PLCs registrados: "
                    + ", ".join(plc_ids)
                    + "."
                ),
                "datos": {"plcs": plc_ids},
                "timestamp": _now_iso(),
            }

        # 5) Si no entendimos la consulta, devolvemos ayuda genérica
        return {
            "respuesta": (
                "No estoy seguro de qué has preguntado. Puedes decirme cosas como: "
                "¿cuál es la presión del motor?, ¿cómo va el tanque 1?, "
                "¿hay alguna alarma activa?, o dame el estado general."
            ),
            "datos": {"query_original": query_raw, "intent_detected": None},
            "timestamp": _now_iso(),
        }

    async def _answer_general_status(self) -> Dict[str, Any]:
        """Lee las variables principales de cada PLC y compone un resumen.

        Las variables a leer se derivan dinámicamente de `config_plcs.json`:
        para cada PLC se toman los HR con `rango_min`/`rango_max` definido
        y los coils de lectura, hasta un máximo de 5 por PLC.
        """
        bloques = []
        datos = {}

        for plc_id, pm in self.plc_managers.items():
            plc_cfg = self.context.get_plc(plc_id)
            if not plc_cfg:
                continue
            bloques.append(f"En {plc_cfg.get('nombre', plc_id)}:")
            datos[plc_id] = {}

            # Selección automática desde la config: HR con rango + coils lectura
            nombres_clave: list = []
            for hr in plc_cfg.get("registros", {}).get("holding_registers", []):
                if "rango_min" in hr and "rango_max" in hr:
                    nombres_clave.append(hr["nombre"])
            for c in plc_cfg.get("registros", {}).get("coils", []):
                nombres_clave.append(c["nombre"])
            nombres_clave = nombres_clave[:5]  # limitar para no saturar el TTS

            for nombre_var in nombres_clave:
                registro = self._find_register_in_plc(plc_cfg, nombre_var)
                if not registro:
                    continue
                # Lectura asíncrona (no bloqueamos el loop)
                lectura = await asyncio.to_thread(
                    pm.read_variable_by_name, nombre_var, registro
                )
                datos[plc_id][nombre_var] = lectura
                bloques.append(
                    f" - {registro.get('descripcion', nombre_var)}: "
                    f"{lectura.get('valor_legible', 'N/D')}"
                )

            n_alarmas = len(plc_cfg.get("alarmas", []))
            if n_alarmas:
                bloques.append(f" ({n_alarmas} alarma(s) configuradas)")

        respuesta = "Estado general de la planta. " + " ".join(bloques)
        return {
            "respuesta": respuesta,
            "datos": datos,
            "timestamp": _now_iso(),
        }

    async def _answer_active_alarms(self, query_raw: str) -> Dict[str, Any]:
        """Lee los coils de alarma de cada PLC y reporta cuáles están activos."""
        bloques = []
        hay_alarma = False
        datos = {}

        for plc_id, pm in self.plc_managers.items():
            plc_cfg = self.context.get_plc(plc_id)
            if not plc_cfg:
                continue
            alarmas = plc_cfg.get("alarmas", [])
            datos[plc_id] = []

            for alarma in alarmas:
                coil_cfg = {"direccion": alarma["coil"], "nombre": alarma["nombre"]}
                valor = await asyncio.to_thread(pm.read_coil, coil_cfg)
                if valor is None:
                    continue
                datos[plc_id].append({"nombre": alarma["nombre"], "activa": bool(valor)})
                if valor:
                    hay_alarma = True
                    bloques.append(
                        f" En {plc_id}: {alarma.get('mensaje', alarma['nombre'])}."
                    )

        if not hay_alarma:
            respuesta = "No hay alarmas activas en este momento. Todo funciona con normalidad."
        else:
            respuesta = "He detectado las siguientes alarmas activas:" + "".join(bloques)

        return {
            "respuesta": respuesta,
            "datos": datos,
            "timestamp": _now_iso(),
        }

    async def _answer_specific_variables(
        self, variables: list, query_raw: str
    ) -> Dict[str, Any]:
        """Lee una o varias variables concretas y reporta su valor."""
        resultados = []
        datos = {}

        for v in variables:
            info = self.context.find_register_by_name(v)
            if not info:
                # Sugerir el nombre real más parecido
                sugerencia = _suggest_close_name(self.context, v)
                resultados.append(f"No conozco una variable llamada «{v}».")
                if sugerencia:
                    resultados.append(f"¿Quisiste decir «{sugerencia}»?")
                datos[v] = None
                continue

            pm = self.plc_managers.get(info["plc_id"])
            if not pm:
                resultados.append(f"No tengo acceso al PLC {info['plc_id']}.")
                continue

            lectura = await asyncio.to_thread(
                pm.read_variable_by_name, v, info
            )
            datos[v] = lectura
            resultados.append(
                f"{info.get('descripcion', v)}: {lectura.get('valor_legible', 'no disponible')}."
            )

        return {
            "respuesta": " ".join(resultados),
            "datos": datos,
            "timestamp": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    #  Detección de ACCIONES pendientes (NO ejecuta, solo encola)        #
    # ------------------------------------------------------------------ #
    async def _detect_pending_action(
        self, query_norm: str
    ) -> Optional[Dict[str, Any]]:
        """
        Detecta si la consulta es una orden de escritura. Si lo es, devuelve
        un dict con los datos para crear un `pending_write`. NO escribe sobre
        el PLC; eso queda diferido a `_execute_pending_write` tras la
        confirmación del operador.

        Retorna:
          * None si la consulta NO es una orden de escritura.
          * dict con clave "respuesta" si la intención fue detectada pero
            faltan datos (ej. rango inválido, número ausente, ON/OFF ambiguo).
            El caller NO debe crear un pending; debe retornar este dict tal cual.
          * dict con clave "cfg" si la acción es válida y debe esperar
            confirmación.
        """
        # Palabras que delatan una intención de escritura/acción
        accion_keywords = [
            "enciende", "apaga", "prende", "apagar", "encender",
            "sube", "baja", "ajusta", "pon", "configura", "establece",
            "cambia", "manda", "envia", "mueve",
        ]
        es_accion = any(k in query_norm for k in accion_keywords)
        if not es_accion:
            return None

        # 1) Detectar variable objetivo (set_punto_velocidad o comando_on_motor)
        accion_objetivo = self._match_action_target(query_norm)
        if accion_objetivo is None:
            return None

        nombre_var, kind = accion_objetivo

        # 2) Buscar la variable escribible en config
        cfg_var = self.context.find_writable_by_name(nombre_var)
        if cfg_var is None:
            return {
                "respuesta": (
                    f"No conozco una variable escribible llamada «{nombre_var}»."
                ),
                "datos": {"intent": "escritura", "variable": nombre_var},
                "timestamp": _now_iso(),
                "accion": None,
            }

        # 3) Extraer el valor según el tipo
        if kind == "set_number":
            valor_raw = self._extract_number(query_norm)
            if valor_raw is None:
                rmin = cfg_var.get("rango_min", "?")
                rmax = cfg_var.get("rango_max", "?")
                unidad = cfg_var.get("unidad", "")
                return {
                    "respuesta": (
                        f"Entendí que quieres ajustar "
                        f"{cfg_var.get('descripcion', nombre_var)}, "
                        f"pero no capturé un número. Indica un valor entre "
                        f"{rmin} y {rmax} {unidad}."
                    ),
                    "datos": {"intent": "escritura", "variable": nombre_var},
                    "timestamp": _now_iso(),
                    "accion": None,
                }
            valor = float(valor_raw)
        elif kind == "bool":
            on_kw = [
                "enciende", "encender", "prende", "prender", "on",
                "arranca", "arrancar", "activa", "activar",
            ]
            off_kw = [
                "apaga", "apagar", "off", "detener", "para", "parar",
                "desactiva", "desactivar",
            ]
            if any(k in query_norm for k in on_kw):
                valor = True
            elif any(k in query_norm for k in off_kw):
                valor = False
            else:
                return {
                    "respuesta": (
                        f"Entendí que quieres actuar sobre "
                        f"{cfg_var.get('descripcion', nombre_var)}, "
                        "pero no quedó claro si encender o apagar. "
                        "Di «enciende» o «apaga»."
                    ),
                    "datos": {"intent": "escritura", "variable": nombre_var},
                    "timestamp": _now_iso(),
                    "accion": None,
                }
        else:
            return None

        # 4) Validar
        err = self.context.validate_writable_value(cfg_var, valor)
        if err is not None:
            return {
                "respuesta": f"No puedo escribir ese valor: {err}.",
                "datos": {
                    "intent": "escritura",
                    "variable": nombre_var,
                    "valor_recibido": valor,
                },
                "timestamp": _now_iso(),
                "accion": None,
            }

        # 5) Verificar acceso al PLCManager (sin escribir todavía)
        pm = self.plc_managers.get(cfg_var["plc_id"])
        if not pm:
            return {
                "respuesta": f"No tengo acceso al PLC {cfg_var['plc_id']}.",
                "datos": {
                    "intent": "escritura",
                    "variable": nombre_var,
                    "valor": valor,
                },
                "timestamp": _now_iso(),
                "accion": None,
            }

        # 6) Construir descripción legible para el TTS de confirmación
        unidad = cfg_var.get("unidad", "")
        if cfg_var["kind"] == "coil":
            tts_accion = "ENCENDER" if valor else "APAGAR"
            tts_descripcion = (
                f"{cfg_var.get('descripcion', nombre_var)} = {tts_accion}"
            )
            valor_legible = tts_accion
        else:
            valor_legible = f"{valor} {unidad}".strip()
            tts_descripcion = (
                f"{cfg_var.get('descripcion', nombre_var)} a "
                f"{valor} {unidad}".strip()
            )

        plc_cfg = self.context.get_plc(cfg_var["plc_id"]) or {}
        plc_nombre = plc_cfg.get("nombre", cfg_var["plc_id"])

        return {
            "cfg": cfg_var,
            "valor": valor,
            "plc_id": cfg_var["plc_id"],
            "variable": nombre_var,
            "kind": cfg_var["kind"],
            "tts_descripcion": tts_descripcion,
            "valor_legible": valor_legible,
            "plc_nombre": plc_nombre,
            "created_at": time.monotonic(),
        }

    def _build_action_aliases(self) -> Dict[str, Dict[str, Any]]:
        """
        Construye el mapeo de variable_escribible -> {keywords, kind} a partir
        de `config_plcs.json`. Si añades una nueva variable escribible al JSON,
        aparece automáticamente aquí.
        """
        # Keywords por nombre conocido (heurísticas en español).
        # Para variables nuevas sin entrada aquí, se usa el nombre con _ -> espacio.
        KEYWORDS_POR_NOMBRE: Dict[str, list] = {
            "set_punto_velocidad": ["velocidad", "frecuencia", "hz", "hercios", "setpoint"],
            "comando_on_motor":    ["motor", "bomba"],
        }
        out: Dict[str, Dict[str, Any]] = {}
        for v in self.context.list_writable_variables():
            nombre = v["nombre"]
            out[nombre] = {
                "variables": KEYWORDS_POR_NOMBRE.get(
                    nombre, [nombre.replace("_", " ")]
                ),
                "kind": "bool" if v.get("kind") == "coil" else "set_number",
                "plc_id": v.get("plc_id"),
            }
        return out

    async def _execute_pending_write(self) -> Dict[str, Any]:
        """Ejecuta la escritura pendiente (llamado tras confirmación)."""
        pending = self._pending_write
        self._pending_write = None

        if pending is None:
            return {
                "respuesta": "No hay acción pendiente para ejecutar.",
                "datos": {"intent": "escritura", "exito": False},
                "timestamp": _now_iso(),
                "accion": None,
            }

        cfg_var = pending["cfg"]
        valor = pending["valor"]
        nombre_var = pending["variable"]
        plc_id = pending["plc_id"]
        pm = self.plc_managers.get(plc_id)

        if pm is None:
            return {
                "respuesta": f"No tengo acceso al PLC {plc_id}.",
                "datos": {
                    "intent": "escritura",
                    "variable": nombre_var,
                    "valor": valor,
                    "plc_id": plc_id,
                    "exito": False,
                },
                "timestamp": _now_iso(),
                "accion": {
                    "tipo": "escritura",
                    "variable": nombre_var,
                    "valor": valor,
                    "exito": False,
                },
            }

        try:
            if cfg_var["kind"] == "coil":
                ok = await asyncio.to_thread(pm.write_coil, cfg_var, bool(valor))
            else:
                ok = await asyncio.to_thread(
                    pm.write_holding_register, cfg_var, valor
                )
        except Exception as e:
            logger.exception(f"Error ejecutando acción confirmada: {e}")
            ok = False

        unidad = cfg_var.get("unidad", "")
        if ok:
            if cfg_var["kind"] == "coil":
                respuesta = (
                    f"He {'encendido' if valor else 'apagado'} el motor."
                )
            else:
                respuesta = (
                    f"He ajustado {cfg_var.get('descripcion', nombre_var)} "
                    f"a {valor} {unidad}.".strip()
                )
        else:
            respuesta = (
                f"No pude escribir en el PLC {plc_id}. "
                "Revisa la conexión Modbus."
            )

        return {
            "respuesta": respuesta,
            "datos": {
                "intent": "escritura",
                "variable": nombre_var,
                "valor": valor,
                "plc_id": plc_id,
                "exito": ok,
                "confirmado": True,
            },
            "timestamp": _now_iso(),
            "accion": {
                "tipo": "escritura",
                "variable": nombre_var,
                "valor": valor,
                "exito": ok,
            },
        }

    def _match_action_target(self, query_norm: str) -> Optional[tuple]:
        """Detecta si la intención apunta a una variable escribible del JSON.

        Itera `self.action_aliases` (derivado de `config_plcs.json` →
        `registros.escribibles`). Si se añade una nueva variable escribible
        al JSON, se detecta automáticamente.
        """
        on_off_kw = [
            "enciende", "encender", "prende", "prender",
            "apaga", "apagar", "arranca", "arrancar", "detener",
            "para", "parar", "activa", "activar", "desactiva", "desactivar",
        ]
        if any(k in query_norm for k in on_off_kw):
            for var_name, spec in self.action_aliases.items():
                if spec["kind"] == "bool":
                    if any(kw in query_norm for kw in spec["variables"]):
                        return (var_name, "bool")
            return None

        set_kw = ["velocidad", "frecuencia", "hz", "hercios"]
        if any(k in query_norm for k in set_kw):
            for var_name, spec in self.action_aliases.items():
                if spec["kind"] == "set_number":
                    if any(kw in query_norm for kw in spec["variables"]):
                        return (var_name, "set_number")
            return None

        return None

    def _extract_number(self, text: str) -> Optional[float]:
        """Extrae el primer número (entero o decimal) presente en el texto."""
        # Acepta "30", "30.5", "30,5"
        m = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if not m:
            return None
        s = m.group(1).replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    # ------------------------------------------------------------------ #
    #  Helpers de PLN (modo local)
    # ------------------------------------------------------------------ #
    def _extract_variables(self, query_norm: str) -> list:
        """
        Encuentra TODAS las variables referenciadas en la consulta,
        tanto por alias coloquiales como por nombre técnico.

        Estrategia: los alias multi-palabra (ej. "tanque 1", "tanque 2")
        se procesan ANTES y son más específicos, así que ganan sobre
        las versiones de una palabra (ej. "tanque").
        """
        encontrados = set()

        # 1) Coincidencias exactas con nombres reales
        for plc in self.context.plcs:
            for hr in plc.get("registros", {}).get("holding_registers", []):
                if hr["nombre"].lower() in query_norm:
                    encontrados.add(hr["nombre"])
            for c in plc.get("registros", {}).get("coils", []):
                if c["nombre"].lower() in query_norm:
                    encontrados.add(c["nombre"])

        # 2) Alias multi-palabra primero (más específicos)
        multi_palabra_matcheados = set()
        alias_multi = sorted(
            (a for a in self.aliases if " " in a),
            key=lambda k: -len(k),
        )
        for alias in alias_multi:
            if alias in query_norm:
                multi_palabra_matcheados.add(alias)
                for v in self.aliases[alias]:
                    encontrados.add(v)

        # 3) Alias de una palabra. Si la palabra ya está cubierta por
        #    un alias multi-palabra, no la procesamos.
        palabras = re.findall(r"\w+", query_norm)
        for palabra in palabras:
            cubierto = any(
                palabra in mp.split() for mp in multi_palabra_matcheados
            )
            if cubierto:
                continue
            for variante in (palabra, palabra.rstrip("s")):  # singular/plural
                if variante in self.aliases:
                    for v in self.aliases[variante]:
                        encontrados.add(v)

        # 3) Si la consulta dice "nivel" y "tanque" y por separado los tres
        # tanques, el set() ya colapsa duplicados: "velocidad_motor" y
        # "corriente_motor" solo aparecen una vez aunque varias keys
        # coloquiales los mencionen.
        return list(encontrados)

    def _find_register_in_plc(self, plc_cfg: Dict[str, Any], nombre_var: str):
        for hr in plc_cfg.get("registros", {}).get("holding_registers", []):
            if hr["nombre"] == nombre_var:
                return {**hr, "kind": "holding_register"}
        for c in plc_cfg.get("registros", {}).get("coils", []):
            if c["nombre"] == nombre_var:
                return {**c, "kind": "coil"}
        return None

    # ------------------------------------------------------------------ #
    #  Modo OPENAI (opcional — requiere API key)
    # ------------------------------------------------------------------ #
    async def _process_openai(self, query: str) -> Dict[str, Any]:
        """
        Punto de integración con OpenAI. Descomentar la importación
        correspondiente en requirements.txt y configurar `OPENAI_API_KEY`.
        """
        try:
            from openai import AsyncOpenAI
        except ImportError:
            logger.warning("openai no instalada; usando modo local como fallback")
            return await self._process_local(query.lower(), query)

        api_key = self.config_global.get("openai_api_key", "")
        if not api_key:
            return {
                "respuesta": "Falta la OPENAI_API_KEY en la configuración.",
                "datos": {},
                "timestamp": _now_iso(),
            }

        client = AsyncOpenAI(api_key=api_key)
        system_prompt = (
            "Eres DANTE Industrial, un asistente de planta experto en PLCs. "
            "Responde de forma breve y clara. Si te preguntan por un valor, "
            "puedes pedirle al operador que confirme el nombre de la variable."
        )
        ctx = self.context.build_context_for_ai()

        completion = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt + "\n" + ctx},
                {"role": "user", "content": query},
            ],
            temperature=0.2,
        )
        text = completion.choices[0].message.content
        return {
            "respuesta": text,
            "datos": {"provider": "openai", "model": "gpt-4o-mini"},
            "timestamp": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    #  Modo ANTHROPIC (opcional)
    # ------------------------------------------------------------------ #
    async def _process_anthropic(self, query: str) -> Dict[str, Any]:
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            logger.warning("anthropic no instalada; usando modo local")
            return await self._process_local(query.lower(), query)

        api_key = self.config_global.get("anthropic_api_key", "")
        if not api_key:
            return {
                "respuesta": "Falta la ANTHROPIC_API_KEY en la configuración.",
                "datos": {},
                "timestamp": _now_iso(),
            }

        # self.model viene de configuracion_global.ia_model en el JSON.
        # Si el JSON tiene un nombre "local" (ej. "DANTE-engine-v1"), usamos
        # el default de Anthropic. Si tiene un claude-*, lo respetamos.
        _ANTHROPIC_DEFAULT = "claude-haiku-4-5"
        model_name = self.model if self.model.startswith("claude-") else _ANTHROPIC_DEFAULT

        client = AsyncAnthropic(api_key=api_key)
        ctx = self.context.build_context_for_ai()
        message = await client.messages.create(
            model=model_name,
            max_tokens=300,
            system=(
                "Eres DANTE Industrial, un asistente experto en PLCs. "
                "Responde de manera concisa y operativa."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"{ctx}\n\nPregunta del operador: {query}",
                }
            ],
        )
        # message.content es una lista de bloques; concatenar todos los text
        text = ""
        for block in message.content:
            if hasattr(block, "text"):
                text += block.text
        return {
            "respuesta": text,
            "datos": {"provider": "anthropic", "model": model_name},
            "timestamp": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    #  Modo SEIUS — endpoint LLM corporativo compatible con Anthropic
    # ------------------------------------------------------------------ #
    async def _process_seius(self, query: str) -> Dict[str, Any]:
        """
        Conecta con el endpoint LLM corporativo de Seius
        (https://llm.seius.com.ec), compatible con la API de Anthropic.
        El token se lee de la variable de entorno `DANTE_SEIUS_LLM_TOKEN`
        (o la antigua `ODIN_SEIUS_LLM_TOKEN`) para no hardcodearlo en el JSON.
        """
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            logger.warning(
                "anthropic no instalada; usando modo local como fallback"
            )
            return await self._process_local(query.lower(), query)

        # 1) Token: variable de entorno (recomendado para no commitearlo)
        api_key = (
            os.environ.get("DANTE_SEIUS_LLM_TOKEN", "").strip()
            or os.environ.get("ODIN_SEIUS_LLM_TOKEN", "").strip()
        )
        if not api_key:
            return {
                "respuesta": (
                    "Falta la variable de entorno DANTE_SEIUS_LLM_TOKEN "
                    "con el token de acceso a llm.seius.com.ec."
                ),
                "datos": {"intent": "llm", "proveedor": "seius", "exito": False},
                "timestamp": _now_iso(),
            }

        # 2) URL base: desde el JSON (configuracion_global.seius_base_url)
        base_url = self.config_global.get(
            "seius_base_url", "https://llm.seius.com.ec"
        ).rstrip("/")

        # 3) Modelo: el JSON manda. Default = "minimax-m3"
        model_name = self.model or "minimax-m3"

        # 4) Parámetros opcionales
        max_tokens = int(self.config_global.get("seius_max_tokens", 300))
        temperature = float(self.config_global.get("seius_temperature", 0.2))

        # 5) Cliente Anthropic apuntando al endpoint corporativo.
        #    La librería acepta `base_url` desde la versión 0.20+.
        client = AsyncAnthropic(api_key=api_key, base_url=base_url)
        ctx = self.context.build_context_for_ai()

        # Construir kwargs base. `temperature` no es aceptado por todas
        # las versiones del SDK de Anthropic: si la versión instalada
        # no la soporta, reintentamos sin ese parámetro.
        create_kwargs = {
            "model": model_name,
            "max_tokens": max_tokens,
            "system": (
                "Eres DANTE Industrial, un asistente experto en PLCs. "
                "Responde en español de manera concisa y operativa.\n\n"
                "IMPORTANTE: tu respuesta será leída por un motor de voz (TTS) "
                "en español, NO por una persona leyendo la pantalla. Por lo tanto:\n"
                "  - NO uses markdown (sin negritas **, sin cursiva _, sin títulos #, "
                "sin listas con guiones -, sin backticks `, sin tablas |).\n"
                "  - NO uses emojis (📊 ⚡ 🛢️ 🛡️ ⚙️ 🚨 ni similares).\n"
                "  - NO uses numeración tipo '1. 2. 3.' porque el TTS dirá "
                "'uno punto' antes de cada elemento.\n"
                "  - Escribe como si hablaras por radio: frases cortas y claras, "
                "separadas por punto y seguido. Sin dos puntos decorativos.\n"
                "  - Para listar variables, sepáralas con comas o con la palabra "
                "'y'. Ejemplo: 'La velocidad es 50 por ciento, la corriente "
                "12 amperios, y la presión 80 libras por pulgada cuadrada.'\n"
                "  - Escribe los números con palabras para unidades habladas: "
                "'50 por ciento' en vez de '50%', 'libras por pulgada cuadrada' "
                "en vez de 'PSI', 'hercios' en vez de 'Hz'.\n"
                "  - Escribe 'Relé' con tilde, no 'Rele'. Escribe 'voltios', "
                "no 'Votios'.\n"
                "  - Si una variable no tiene lectura, di 'no disponible' "
                "sin más, sin corchetes ni comillas.\n"
                "  - Máximo 3-4 frases por respuesta. Sé breve."
            ),
            "messages": [
                {
                    "role": "user",
                    "content": f"{ctx}\n\nPregunta del operador: {query}",
                }
            ],
        }
        try:
            message = await client.messages.create(
                **create_kwargs,
                temperature=temperature,
            )
        except TypeError as te:
            # SDK reciente (>=0.40) que no acepta `temperature` aquí.
            # LiteLLM acepta omitirla y aplica su default.
            if "temperature" in str(te):
                logger.debug(
                    "SDK anthropic no soporta 'temperature' en messages.create(); "
                    "reintentando sin ese parámetro."
                )
                message = await client.messages.create(**create_kwargs)
            else:
                raise
        except Exception as e:
            logger.exception(f"Error llamando a {base_url} ({model_name}): {e}")
            return {
                "respuesta": (
                    f"No pude contactar el servicio de IA en {base_url}. "
                    "Revisa la conexión y el token DANTE_SEIUS_LLM_TOKEN."
                ),
                "datos": {
                    "intent": "llm",
                    "proveedor": "seius",
                    "model": model_name,
                    "base_url": base_url,
                    "error": str(e),
                },
                "timestamp": _now_iso(),
            }

        # message.content es una lista de bloques; concatenar todos los text
        text = ""
        for block in message.content:
            if hasattr(block, "text"):
                text += block.text

        return {
            "respuesta": text,
            "datos": {
                "provider": "seius",
                "model": model_name,
                "base_url": base_url,
            },
            "timestamp": _now_iso(),
        }


# ---------------------------------------------------------------------- #
#  Utilidades
# ---------------------------------------------------------------------- #
def _classify_confirmation(query_norm: str) -> str:
    """
    Clasifica la respuesta del operador ante un pending_write.
    Devuelve 'confirm' | 'cancel' | 'other'.
    """
    tokens = set(re.findall(r"[a-záéíóúñü]+", query_norm))
    if tokens & _CONFIRM_WORDS:
        return "confirm"
    if tokens & _CANCEL_WORDS:
        return "cancel"
    return "other"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _suggest_close_name(context: AIContextManager, term: str) -> Optional[str]:
    """Búsqueda difusa simple: sugiere el nombre más parecido (Levenshtein)."""
    nombres = []
    for plc in context.plcs:
        for hr in plc.get("registros", {}).get("holding_registers", []):
            nombres.append(hr["nombre"])
        for c in plc.get("registros", {}).get("coils", []):
            nombres.append(c["nombre"])

    if not nombres:
        return None

    term_l = term.lower()
    mejor = min(nombres, key=lambda n: _levenshtein(term_l, n.lower()))
    if _levenshtein(term_l, mejor.lower()) <= 4:
        return mejor
    return None


def _levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(b) + 1):
        dp[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(
                    dp[i - 1][j],      # deleción
                    dp[i][j - 1],      # inserción
                    dp[i - 1][j - 1],  # sustitución
                )
    return dp[len(a)][len(b)]
