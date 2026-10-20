"""
webhook_bridge.py
=================
Clase: WebhookBridge

Worker asíncrono que monitorea periódicamente cada PLC configurado.
Detecta flancos de subida en coils de alarma y notifica al
WebSocketManager para que la interfaz web los anuncie de inmediato.

También expone un método para inyectar alarmas vía webhook externo.
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

from plc_manager import PLCManager
from ai_context_manager import AIContextManager
from websocket_manager import WebSocketManager

logger = logging.getLogger("DANTEIndustrial.WebhookBridge")


class WebhookBridge:
    """
    Bucle en background que sondea los PLCs en busca de alarmas.
    """

    def __init__(
        self,
        context: AIContextManager,
        ws_manager: WebSocketManager,
        poll_interval_s: float = 1.0,
    ):
        self.context = context
        self.ws_manager = ws_manager
        self.poll_interval_s = poll_interval_s
        self.plc_managers: Dict[str, PLCManager] = {}

        # Cola para webhooks externos / inyecciones manuales
        self._external_queue: asyncio.Queue = asyncio.Queue()

        self._task: Optional[asyncio.Task] = None
        self._running: bool = False

        # Construir PLCManager por cada PLC configurado
        for plc_cfg in self.context.plcs:
            self.plc_managers[plc_cfg["plc_id"]] = PLCManager(plc_cfg)

    # ------------------------------------------------------------------ #
    #  Ciclo de vida
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Arranca la tarea en background."""
        if self._task and not self._task.done():
            logger.warning("WebhookBridge ya está corriendo.")
            return

        self._running = True
        self._task = asyncio.create_task(self._loop(), name="webhook-bridge-loop")
        logger.info("WebhookBridge iniciado.")

    async def stop(self) -> None:
        """Detiene la tarea y desconecta todos los PLCs."""
        self._running = False
        task = self._task
        self._task = None
        if task and not task.done():
            # Pedir cancelación y esperar fuera del try
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Loop terminó con: {e}")

        for pm in self.plc_managers.values():
            try:
                pm.disconnect()
            except Exception:
                pass
        logger.info("WebhookBridge detenido.")

    # ------------------------------------------------------------------ #
    #  Loop principal
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        """Bucle que se ejecuta indefinidamente mientras _running=True."""
        logger.info(
            f"WebhookBridge en polling cada {self.poll_interval_s}s "
            f"sobre {len(self.plc_managers)} PLC(s)"
        )

        while self._running:
            try:
                # 1) Procesar webhooks externos encolados
                await self._drain_external_queue()

                # 2) Sondeo de PLCs
                for pm in list(self.plc_managers.values()):
                    await self._poll_plc(pm)

            except asyncio.CancelledError:
                logger.info("Loop cancelado, saliendo...")
                break
            except Exception as e:
                logger.exception(f"Error inesperado en loop: {e}")

            await asyncio.sleep(self.poll_interval_s)

    async def _poll_plc(self, pm: PLCManager) -> None:
        """Sondea UN PLC para detectar flancos de subida en alarmas."""
        alarmas_cfg = self.context.get_alarms_for_plc(pm.plc_id)
        if not alarmas_cfg:
            return

        # Conexión / reconexión si es necesario
        if not pm.is_connected():
            connected = await asyncio.to_thread(pm.connect)
            if not connected:
                # El PLC no responde. Silenciar este ciclo;
                # el PLCManager ya rate-limitea sus propios logs.
                return

        # Lectura de flancos — se hace en un thread para no bloquear el loop
        try:
            rising = await asyncio.to_thread(pm.detect_rising_edges, alarmas_cfg)
        except Exception as e:
            logger.error(f"[{pm.plc_id}] Error en lectura de alarmas: {e}")
            return

        # Difundir cada alarma detectada
        for alarm in rising:
            alarm_payload = {
                **alarm,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "origen_plc": pm.nombre,
            }
            logger.warning(
                f"🚨 ALARMA [{pm.plc_id}] {alarm['nombre']} - sev={alarm['severidad']}"
            )
            await self.ws_manager.broadcast_alarm(alarm_payload)

    # ------------------------------------------------------------------ #
    #  Webhook / inyección externa
    # ------------------------------------------------------------------ #
    async def push_external_event(self, event: Dict[str, Any]) -> None:
        """
        Recibe un evento externo (de un webhook HTTP, otro sistema, etc.)
        y lo encola para difundirlo en el próximo ciclo.
        """
        await self._external_queue.put(event)

    async def _drain_external_queue(self) -> None:
        while not self._external_queue.empty():
            try:
                event = self._external_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self.ws_manager.broadcast(
                {
                    "tipo": "webhook",
                    **event,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
            )

    # ------------------------------------------------------------------ #
    #  Acceso directo (utilidad)
    # ------------------------------------------------------------------ #
    def get_plc_manager(self, plc_id: str) -> Optional[PLCManager]:
        return self.plc_managers.get(plc_id)
