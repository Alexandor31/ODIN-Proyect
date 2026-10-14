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
"""

import re
import logging
import asyncio
from typing import Dict, Any, Optional

from ai_context_manager import AIContextManager
from plc_manager import PLCManager

logger = logging.getLogger("JarvisIndustrial.IndustrialAgentService")


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
        self.model = self.config_global.get("ia_model", "jarvis-engine-v1")

        # Diccionario de sinónimos /alias en español para mapear nombres coloquiales
        # a nombres reales de variables en el PLC.
        self.aliases: Dict[str, list] = {
            # temperatura
            "temperatura": ["temperatura_motor", "setpoint_temperatura"],
            "calor": ["temperatura_motor"],
            "caliente": ["temperatura_motor"],
            # presión
            "presion": ["presion_sistema"],
            "presión": ["presion_sistema"],
            "bar": ["presion_sistema"],
            # velocidad
            "velocidad": ["velocidad_linea"],
            "rpm": ["velocidad_linea"],
            "linea": ["velocidad_linea"],
            # producción
            "produccion": ["contador_produccion"],
            "producción": ["contador_produccion"],
            "contador": ["contador_produccion"],
            "piezas": ["contador_produccion"],
            # motores / bombas
            "motor": ["motor_principal"],
            "bomba": ["bomba_hidraulica"],
            "hidraulica": ["bomba_hidraulica"],
            "hidráulica": ["bomba_hidraulica"],
            "banda": ["banda_transportadora"],
            # tanques
            "nivel": ["nivel_tanque"],
            "tanque": ["nivel_tanque"],
            "flujo": ["flujo_embalaje"],
            # alarmas
            "alarma": ["alarma_temperatura", "alarma_presion", "alarma_emergencia"],
            "emergencia": ["alarma_emergencia"],
            # setpoints
            "setpoint": ["setpoint_temperatura"],
            "ajuste": ["setpoint_temperatura"],
        }

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
            "timestamp":  "ISO-8601"
          }
        """
        history = history or []
        query_norm = query.strip().lower()

        try:
            if self.provider == "local":
                return await self._process_local(query_norm, query)
            elif self.provider == "openai":
                return await self._process_openai(query)
            elif self.provider == "anthropic":
                return await self._process_anthropic(query)
            else:
                logger.warning(f"Proveedor '{self.provider}' desconocido, fallback a local")
                return await self._process_local(query_norm, query)
        except Exception as e:
            logger.exception(f"Error procesando consulta: {e}")
            return {
                "respuesta": (
                    "Disculpa, ocurrió un error al procesar tu consulta. "
                    "Revisa el log del servidor."
                ),
                "datos": {"error": str(e)},
                "timestamp": _now_iso(),
            }

    # ------------------------------------------------------------------ #
    #  Modo LOCAL (reglas + lectura real)
    # ------------------------------------------------------------------ #
    async def _process_local(self, query_norm: str, query_raw: str) -> Dict[str, Any]:
        """
        En modo local:
          - Identifica la intención (estado general, valor concreto, alarma, setpoint).
          - Si es un valor concreto, lee el PLC en vivo.
          - Si es una intención global, resume todos los PLCs.
        """

        # Intención: estado general
        if any(k in query_norm for k in ["estado", "resumen", "general", "como va", "cómo va"]):
            return await self._answer_general_status()

        # Intención: ¿qué alarmas hay activas?
        if any(k in query_norm for k in ["alarma", "alertas", "problemas", "fallos"]):
            return await self._answer_active_alarms(query_raw)

        # ¿Cuántos PLCs?
        if any(k in query_norm for k in ["cuantos", "cuántos", "lista de plc", "qué plc"]):
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

        # Buscar variables mencionadas (por alias o por nombre)
        variables = self._extract_variables(query_norm)

        if variables:
            return await self._answer_specific_variables(variables, query_raw)

        # Si no entendimos la consulta, devolvemos ayuda genérica
        return {
            "respuesta": (
                "No estoy seguro de qué has preguntado. Puedes decirme cosas como: "
                "¿cuál es la temperatura del motor?, ¿cómo va la presión?, "
                "¿hay alguna alarma activa?, o dame el estado general."
            ),
            "datos": {"query_original": query_raw, "intent_detected": None},
            "timestamp": _now_iso(),
        }

    async def _answer_general_status(self) -> Dict[str, Any]:
        """Lee todas las variables principales de cada PLC y compone un resumen."""
        bloques = []
        datos = {}

        # Selección de variables representativas para el resumen
        variables_clave_por_plc = {
            "PLC_LINEA_01": ["temperatura_motor", "presion_sistema", "velocidad_linea"],
            "PLC_LINEA_02": ["nivel_tanque", "flujo_embalaje"],
        }

        for plc_id, pm in self.plc_managers.items():
            plc_cfg = self.context.get_plc(plc_id)
            if not plc_cfg:
                continue
            nombres = variables_clave_por_plc.get(plc_id, [])
            bloques.append(f"En {plc_cfg.get('nombre', plc_id)}:")
            datos[plc_id] = {}

            for nombre_var in nombres:
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
    #  Helpers de PLN (modo local)
    # ------------------------------------------------------------------ #
    def _extract_variables(self, query_norm: str) -> list:
        """
        Encuentra TODAS las variables referenciadas en la consulta,
        tanto por alias coloquiales como por nombre técnico.
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

        # 2) Alias coloquiales
        palabras = re.findall(r"\w+", query_norm)
        for palabra in palabras:
            for variante in (palabra, palabra.rstrip("s")):  # singular/plural
                if variante in self.aliases:
                    for v in self.aliases[variante]:
                        encontrados.add(v)

        # 3) Si hay palabras como "temperatura" pero ya detectamos
        # "temperatura_motor" o "setpoint_temperatura", no las devolvemos
        # dos veces: dedupe por nombre canónico.
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
            "Eres Jarvis Industrial, un asistente de planta experto en PLCs. "
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

        client = AsyncAnthropic(api_key=api_key)
        ctx = self.context.build_context_for_ai()
        message = await client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=300,
            system=(
                "Eres Jarvis Industrial, un asistente experto en PLCs. "
                "Responde de manera concisa y operativa."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"{ctx}\n\nPregunta del operador: {query}",
                }
            ],
        )
        # message.content es una lista de bloques; tomar el primer text
        text = ""
        for block in message.content:
            if hasattr(block, "text"):
                text += block.text
        return {
            "respuesta": text,
            "datos": {"provider": "anthropic", "model": "claude-3-5-haiku-20241022"},
            "timestamp": _now_iso(),
        }


# ---------------------------------------------------------------------- #
#  Utilidades
# ---------------------------------------------------------------------- #
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
