"""
websocket_manager.py
====================
Clase: WebSocketManager

Administra múltiples conexiones WebSocket activas con clientes web
(la interfaz HTML/JS). Permite difundir mensajes (alarmas, eventos,
respuestas) de forma masiva.
"""

import asyncio
import logging
from typing import Set, Dict, Any

from fastapi import WebSocket

logger = logging.getLogger("JarvisIndustrial.WebSocketManager")


class WebSocketManager:
    """
    Mantiene un set de clientes WS conectados y un lock para evitar
    race-conditions al enviar mensajes concurrentemente.
    """

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  Conexión / desconexión
    # ------------------------------------------------------------------ #
    async def connect(self, websocket: WebSocket) -> None:
        """Acepta y registra una nueva conexión WebSocket."""
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
        logger.info(
            f"Cliente WS conectado. Total: {len(self.active_connections)}"
        )

        # Saludo inicial
        await self._send_personal(
            websocket,
            {
                "tipo": "sistema",
                "nivel": "info",
                "mensaje": "Conectado a Jarvis Industrial. Esperando eventos...",
            },
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Elimina una conexión del set activo."""
        async with self._lock:
            self.active_connections.discard(websocket)
        logger.info(
            f"Cliente WS desconectado. Total: {len(self.active_connections)}"
        )

    # ------------------------------------------------------------------ #
    #  Métodos de envío
    # ------------------------------------------------------------------ #
    async def _send_personal(self, websocket: WebSocket, message: Dict[str, Any]) -> None:
        """Envía un mensaje a UN cliente (uso interno)."""
        try:
            await websocket.send_json(message)
        except Exception as e:
            logger.debug(f"Error enviando a cliente WS: {e}")

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Envía el mismo mensaje a TODOS los clientes conectados."""
        async with self._lock:
            connections = list(self.active_connections)

        # Importante: hacer la copia fuera del lock y enviar sin lockear
        for connection in connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.debug(f"Error en broadcast a un cliente: {e}")
                # Si falla, lo retiramos
                await self._safe_remove(connection)

    async def _safe_remove(self, websocket: WebSocket) -> None:
        try:
            await self.disconnect(websocket)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Atajos de broadcasting semánticos
    # ------------------------------------------------------------------ #
    async def broadcast_alarm(self, alarm_data: Dict[str, Any]) -> None:
        """
        Difunde una alarma al frontend en un formato estándar.
        El frontend la reproducirá por TTS automáticamente.
        """
        payload = {
            "tipo": "alarma",
            "origen": "plc",
            **alarm_data,
        }
        await self.broadcast(payload)

    async def broadcast_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Mensaje genérico del sistema."""
        await self.broadcast(
            {
                "tipo": "evento",
                "subtipo": event_type,
                **data,
            }
        )

    @property
    def client_count(self) -> int:
        return len(self.active_connections)
