"""
main.py
=======
Punto de entrada de "DANTE Industrial".

Levanta el servidor FastAPI con:
  * POST /api/voice-query       -> consulta por voz
  * POST /api/webhook-receiver  -> webhook entrante externo
  * GET  /api/plcs              -> lista de PLCs configurados
  * GET  /api/health            -> healthcheck
  * WS   /ws                    -> canal en tiempo real con la UI

Inicializa todas las clases del sistema y arranca el worker en background.
"""

import os
import platform
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Any

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
    Body,
    Depends,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ai_context_manager import AIContextManager
from plc_manager import PLCManager
from webhook_bridge import WebhookBridge
from websocket_manager import WebSocketManager
from industrial_agent_service import IndustrialAgentService


def env(name: str, legacy: str, default: str) -> str:
    """Lee una variable de entorno DANTE_* y, si no existe, la ODIN_*
    equivalente. Así los despliegues anteriores siguen funcionando
    tras el renombrado."""
    return os.getenv(name) or os.getenv(legacy) or default


# ---------------------------------------------------------------------- #
#  Configuración de logging
# ---------------------------------------------------------------------- #
LOG_LEVEL = env("DANTE_LOG_LEVEL", "ODIN_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# Silenciar el logger de pymodbus: repite "Connection failed..." en bucle
# mientras el PLC no responde. Solo mostraremos CRITICAL y superior.
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)
logging.getLogger("pymodbus.logging").setLevel(logging.CRITICAL)
logger = logging.getLogger("DANTEIndustrial.Main")

# ---------------------------------------------------------------------- #
#  Modelos Pydantic (validación de entrada/salida)
# ---------------------------------------------------------------------- #
class VoiceQueryRequest(BaseModel):
    query: str = Field(..., description="Texto transcrito de la voz del operador")
    plc_id: str | None = Field(None, description="PLC objetivo (opcional)")
    history: list = Field(default_factory=list, description="Historial reciente")


class VoiceQueryResponse(BaseModel):
    respuesta: str
    datos: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str
    success: bool = True


class WebhookPayload(BaseModel):
    tipo: str = Field("evento", description="Tipo de evento externo")
    mensaje: str = Field(..., description="Mensaje a difundir")
    severidad: str = Field("info", description="info | media | alta | critica")
    origen: str | None = Field(None, description="Sistema que origina el webhook")


class HealthResponse(BaseModel):
    status: str
    plcs_conectados: int
    plcs_totales: int
    plcs_detalle: list = Field(default_factory=list)
    ws_clientes: int
    ia_provider: str | None = None
    puerto: int
    nota: str | None = None


# ---------------------------------------------------------------------- #
#  Ciclo de vida (startup / shutdown)
# ---------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Reemplazo moderno del antiguo @app.on_event("startup").
    Inicializa las clases y arranca el worker de monitoreo.
    """
    banner = (
        "\n"
        "+=================================================================+\n"
        "|                     O D I N   I N D U S T R I A L              |\n"
        "+=================================================================+\n"
    )
    print(banner)
    logger.info("Iniciando DANTE Industrial…")

    # 1) Contexto (configuración)
    config_path = env("DANTE_CONFIG", "ODIN_CONFIG", "config_plcs.json")
    context = AIContextManager(config_path)

    # 2) Gestor de WebSockets
    ws_manager = WebSocketManager()

    # 3) Construir los PLCManager para cada PLC
    plc_managers: Dict[str, PLCManager] = {}
    for plc_cfg in context.plcs:
        pm = PLCManager(plc_cfg)
        plc_managers[plc_cfg["plc_id"]] = pm

    # 4) Worker de monitoreo (WebhookBridge)
    poll_interval_ms = context.get_global_config().get("poll_interval_ms", 1000)
    bridge = WebhookBridge(
        context=context,
        ws_manager=ws_manager,
        poll_interval_s=max(0.2, poll_interval_ms / 1000.0),
    )
    bridge.start()

    # 5) Servicio de IA
    agent = IndustrialAgentService(context, plc_managers)

    # Guardar en `app.state` para acceso desde los endpoints
    app.state.context = context
    app.state.ws_manager = ws_manager
    app.state.plc_managers = plc_managers
    app.state.bridge = bridge
    app.state.agent = agent

    # 6) Aviso claro de la URL (imprime al stdout SIN pasar por el logger
    #    para evitar competir con uvicorn).
    host = env("DANTE_HOST", "ODIN_HOST", "0.0.0.0")
    port = int(env("DANTE_PORT", "ODIN_PORT", "8000"))
    # La consola se sirve en local, sin cifrar.
    _scheme = "http"
    url = f"{_scheme}://localhost:{port}"

    # Intentar detectar IPs locales para dar pistas cuando el server
    # corre dentro de WSL o un contenedor.
    ips = []
    try:
        import socket
        hostname = socket.gethostname()
        # hostname puede no resolver; probar también por interfaz
        try:
            ips.append(socket.gethostbyname(hostname))
        except Exception:
            pass
        # conectar a una IP pública NO envía paquetes: solo deduce la iface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass

    ip_hint = ""
    if ips:
        ip_hint = "\n  Si abres la UI desde otra máquina o desde Windows\n  y el server corre en WSL/Linux, prueba con:"
        for ip in sorted(set(ips)):
            ip_hint += f"\n      {_scheme}://{ip}:{port}"

    print(
        f"\n  Consola web disponible en:\n"
        f"    >>> {url} <<<{ip_hint}\n"
        f"\n"
        f"  Diagnóstico:  GET  {url}/api/_whoami\n"
        f"  Health:       GET  {url}/api/health\n"
        f"  PLCs:         GET  {url}/api/plcs\n"
        f"\n"
        f"  TTS/STT:   requiere navegador Chrome/Edge (Web Speech API).\n"
        + "-" * 67
    )

    try:
        yield
    finally:
        # Apagado robusto: capturar CancelledError (proveniente de Ctrl+C)
        try:
            logger.info("Apagando DANTE Industrial…")
            await bridge.stop()
        except (asyncio.CancelledError, Exception) as e:
            logger.debug(f"Bridge stop con excepción: {e}")
        try:
            for pm in plc_managers.values():
                pm.disconnect()
        except Exception as e:
            logger.debug(f"PM disconnect con excepción: {e}")
        logger.info("Apagado completo.")


# ---------------------------------------------------------------------- #
#  Aplicación FastAPI
# ---------------------------------------------------------------------- #
app = FastAPI(
    title="DANTE Industrial",
    description="Puente web inteligente PLC ↔ Asistente IA para voz y alarmas",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------- #
#  CORS                                                                #
# ---------------------------------------------------------------------- #
# Antes era allow_origins=["*"] con allow_credentials=True, combinación
# que la spec de CORS prohíbe y que el navegador ignora cuando hay
# credenciales. La consola se sirve desde el propio servidor, así que
# el único origen legítimo es el propio servidor: cualquier otro se
# rechaza en el navegador antes de llegar a la red de planta.
_cors_origins = [
    o.strip()
    for o in env("DANTE_CORS_ORIGINS", "ODIN_CORS_ORIGINS", "").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,   # vacío = misma origen únicamente
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------- #
#  Dependencias (acceden al estado guardado en app.state)
# ---------------------------------------------------------------------- #
def get_agent() -> IndustrialAgentService:
    return app.state.agent


def get_ws_manager() -> WebSocketManager:
    return app.state.ws_manager


def get_bridge() -> WebhookBridge:
    return app.state.bridge


def get_context() -> AIContextManager:
    return app.state.context


# ---------------------------------------------------------------------- #
#  ENDPOINTS REST
# ---------------------------------------------------------------------- #
@app.get("/api/health", response_model=HealthResponse, tags=["meta"])
async def health():
    """Estado general del sistema."""
    plcs = app.state.plc_managers
    conectados = sum(1 for pm in plcs.values() if pm.is_connected())
    detalle = [
        {
            "plc_id": pm.plc_id,
            "ip": pm.ip,
            "port": pm.port,
            "conectado": pm.is_connected(),
        }
        for pm in plcs.values()
    ]
    nota = None
    if conectados == 0 and plcs:
        nota = (
            "Ningún PLC responde todavía. La aplicación funciona en "
            "modo simulación. Revisa IPs/puertos en config_plcs.json "
            "y que el firewall permita el puerto Modbus."
        )
    elif conectados < len(plcs):
        nota = (
            f"Solo {conectados}/{len(plcs)} PLC(s) responden. "
            f"El resto opera en modo simulación."
        )
    return HealthResponse(
        status="ok",
        plcs_conectados=conectados,
        plcs_totales=len(plcs),
        plcs_detalle=detalle,
        ws_clientes=app.state.ws_manager.client_count,
        ia_provider=app.state.context.get_global_config().get("ia_provider", "local"),
        puerto=int(env("DANTE_PORT", "ODIN_PORT", "8000")),
        nota=nota,
    )


@app.get("/api/plcs", tags=["meta"])
async def list_plcs():
    """Lista de PLCs configurados (resumen)."""
    return {
        "plcs": app.state.context.get_all_plcs_summary(),
        "config_global": app.state.context.get_global_config(),
    }


@app.get("/api/_whoami", tags=["meta"])
async def whoami():
    """Diagnóstico de red: IPs desde las que el server es accesible."""
    import socket
    info = {
        "hostname": socket.gethostname(),
        "ips": [],
        "host_bind": env("DANTE_HOST", "ODIN_HOST", "0.0.0.0"),
        "port": int(env("DANTE_PORT", "ODIN_PORT", "8000")),
        "urls_sugeridas": [],
    }
    # hostname
    try:
        info["ips"].append(socket.gethostbyname(socket.gethostname()))
    except Exception:
        pass
    # IP de la interfaz por defecto
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        info["ips"].append(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    for ip in sorted(set(info["ips"])):
        info["urls_sugeridas"].append(f"http://{ip}:{info['port']}")

    # Si además corre en WSL, sugerir el reenvío de puertos
    info["en_wsl"] = os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop") or \
        "microsoft" in platform.uname().release.lower()

    return info


@app.post("/api/voice-query", response_model=VoiceQueryResponse, tags=["agente"])
async def voice_query(req: VoiceQueryRequest):
    """
    Recibe una consulta de voz (ya transcrita) y devuelve una respuesta
    adecuada para ser sintetizada por TTS.
    """
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="La consulta no puede estar vacía")

    agent: IndustrialAgentService = app.state.agent
    result = await agent.process_query(req.query, history=req.history)

    # Difundir también por WebSocket (para mantener un log en la UI)
    await app.state.ws_manager.broadcast_event(
        "respuesta_ia",
        {
            "pregunta": req.query,
            "respuesta": result["respuesta"],
        },
    )

    return VoiceQueryResponse(**result)


@app.post("/api/webhook-receiver", tags=["webhook"])
async def webhook_receiver(payload: WebhookPayload = Body(...)):
    """
    Endpoint para recibir eventos/alarmas desde sistemas externos
    (correo, sistemas MES, otros PLCs, etc.). Reenvía por WebSocket.
    """
    payload_dict = payload.model_dump()
    bridge: WebhookBridge = app.state.bridge
    await bridge.push_external_event(payload_dict)
    return {"ok": True, "mensaje": "evento encolado para difusión"}


@app.get("/api/writable", tags=["control"])
async def list_writable():
    """Lista todas las variables escribibles configuradas."""
    return {"writables": app.state.context.list_writable_variables()}


# ---------------------------------------------------------------------- #
#  WebSocket
# ---------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Canal bidireccional con la UI. Por defecto solo recibe eventos."""
    ws_manager: WebSocketManager = app.state.ws_manager
    await ws_manager.connect(websocket)
    try:
        # Mantener viva la conexión; opcionalmente recibir comandos del cliente
        while True:
            data = await websocket.receive_text()
            logger.debug(f"WS <- cliente: {data}")
            # Eco mínimo para mantener viva la conexión
            await websocket.send_json({"tipo": "ack", "echo": data})
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WS error: {e}")
        await ws_manager.disconnect(websocket)


# ---------------------------------------------------------------------- #
#  Frontend (HTML autocontenido)
# ---------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
async def root():
    """Sirve la interfaz web."""
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if not os.path.exists(html_path):
        return JSONResponse(
            {"error": "No se encontró index.html en el directorio del servidor"},
            status_code=404,
        )
    return FileResponse(html_path, media_type="text/html")


# ---------------------------------------------------------------------- #
#  Lanzamiento directo
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn

    host = env("DANTE_HOST", "ODIN_HOST", "0.0.0.0")
    port = int(env("DANTE_PORT", "ODIN_PORT", "8000"))

    # access_log=False evita inundar la consola con una línea por cada GET.
    # Los logs importantes se ven por el logger "DANTEIndustrial.*" y el
    # banner de arranque que imprime main.py.
    try:
        uvicorn.run(
            "main:app",
            host=host,
            port=port,
            reload=False,           # True sólo en desarrollo
            log_level="warning",    # sólo warnings y errores de uvicorn
            access_log=False,
        )
    except KeyboardInterrupt:
        # Salida limpia al pulsar Ctrl+C
        print("\nDANTE Industrial detenido por el usuario.")
    except SystemExit:
        pass
