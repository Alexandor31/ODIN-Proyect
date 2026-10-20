"""
plc_manager.py
==============
Clase: PLCManager

Encapsula la comunicación Modbus TCP con los PLCs. Usa `pymodbus`
para leer holding registers y coils, con manejo de reconexión
automática y decodificación de float32 / uint32 / uint16.
"""

import struct
import time
import logging
from typing import Dict, Any, List, Optional

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException, ConnectionException

try:
    from pymodbus.__init__ import __version__ as _PM_VERSION
except Exception:  # pragma: no cover
    _PM_VERSION = "0.0.0"

logger = logging.getLogger("DANTEIndustrial.PLCManager")

# Tiempo que se espera tras un fallo de conexión antes de reintentarlo.
# Evita que cada lectura de una consulta se lleve timeout × retries.
_CONNECT_COOLDOWN_S = 8.0

# ------------------------------------------------------------------ #
#  Compatibilidad pymodbus
#  3.5.x y anteriores -> usan kwarg "slave=<unit_id>"
#  3.6.x introdujo "device_id" como deprecado, todavía aceptaba "slave"
#  3.7+ removieron "slave" completamente -> solo "device_id"
#  Detectamos una vez al importar para no pagar el coste por cada lectura.
# ------------------------------------------------------------------ #
def _parse_version(v: str):
    parts = v.split(".")
    nums = []
    for p in parts[:3]:
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


_PM_TUPLE = _parse_version(_PM_VERSION)
_USE_DEVICE_ID = _PM_TUPLE >= (3, 7, 0)
_UNIT_KWARG = "device_id" if _USE_DEVICE_ID else "slave"

logger.debug(
    "pymodbus %s detectado -> usando kwarg '%s' para unit/slave id",
    _PM_VERSION,
    _UNIT_KWARG,
)


def _now_ts() -> float:
    return time.time()


class PLCManager:
    """
    Administra una conexión Modbus TCP con un PLC específico.
    Permite leer registros holding y coils según la configuración
    proveída en el AIContextManager.
    """

    # ------------------------------------------------------------------ #
    #  Inicialización
    # ------------------------------------------------------------------ #
    def __init__(self, plc_config: Dict[str, Any]):
        self.plc_id: str = plc_config["plc_id"]
        self.ip: str = plc_config["ip"]
        self.port: int = plc_config.get("port", 502)
        self.unit_id: int = plc_config.get("unit_id", 1)
        self.nombre: str = plc_config.get("nombre", self.plc_id)

        self.client: Optional[ModbusTcpClient] = None
        self._connected: bool = False
        self._connect_fail_ts: float = 0.0

        # Caché liviano del último estado (utilizado por el worker para
        # detectar flancos de subida en alarmas).
        self._last_states: Dict[str, bool] = {}

        logger.info(
            f"PLCManager inicializado para {self.plc_id} ({self.ip}:{self.port})"
        )

    # ------------------------------------------------------------------ #
    #  Conexión / desconexión
    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        """Establece (o re-establece) la conexión Modbus TCP.

        Si el PLC no responde, no se reintenta en cada lectura: un
        connect() fallido cuesta timeout × retries (3 s × 2), y una
        consulta que lee 4 variables tardaba más de 12 s en responder.
        Con el enfriamiento fallamos rápido y seguimos en modo
        simulación hasta que venza.
        """
        # Enfriamiento: si acabamos de fallar, no reintentar todavía.
        cooldown = getattr(self, "_connect_fail_ts", 0.0)
        if cooldown and (_now_ts() - cooldown) < _CONNECT_COOLDOWN_S:
            self._connected = False
            return False

        try:
            if self.client is None:
                self.client = ModbusTcpClient(
                    host=self.ip,
                    port=self.port,
                    timeout=3,
                    retries=2,
                )

            if not self.client.connected:
                self._connected = self.client.connect()

            if self._connected:
                self._connect_fail_ts = 0.0
                logger.info(f"[{self.plc_id}] Conectado a {self.ip}:{self.port}")
            else:
                self._connect_fail_ts = _now_ts()
                # Rate-limit del warning: máximo uno cada N segundos
                # para evitar spam en el log cuando el PLC está offline.
                if not getattr(self, "_last_warn_ts", 0) or (
                    _now_ts() - self._last_warn_ts
                ) > getattr(self, "_warn_interval_s", 15):
                    logger.info(
                        f"[{self.plc_id}] PLC no disponible en {self.ip}:{self.port} "
                        f"-> modo simulación (sin PLC físico)"
                    )
                    self._last_warn_ts = _now_ts()
                else:
                    logger.debug(
                        f"[{self.plc_id}] PLC sigue sin responder (reintento silencioso)"
                    )

            return self._connected

        except Exception as e:
            logger.error(f"[{self.plc_id}] Error al conectar: {e}")
            self._connected = False
            self._connect_fail_ts = _now_ts()
            return False

    def disconnect(self) -> None:
        try:
            if self.client and self.client.connected:
                self.client.close()
            logger.info(f"[{self.plc_id}] Desconectado")
        except Exception as e:
            logger.warning(f"[{self.plc_id}] Error cerrando conexión: {e}")
        finally:
            self._connected = False
            self.client = None

    def is_connected(self) -> bool:
        """Devuelve True si la conexión Modbus está activa."""
        return bool(self._connected and self.client and self.client.connected)

    # ------------------------------------------------------------------ #
    #  Decodificación de registros
    # ------------------------------------------------------------------ #
    @staticmethod
    def _decode_value(registers: List[int], tipo: str) -> Any:
        """
        Decodifica una lista de registros Modbus (16 bits cada uno)
        al tipo Python correspondiente: 'uint16', 'uint32', 'float32'.
        """
        if not registers:
            return 0

        if tipo == "uint16":
            return registers[0]

        if tipo == "int16":
            v = registers[0]
            return v - 0x10000 if v & 0x8000 else v

        if tipo == "uint32":
            # Big-endian: palabra alta primero
            return (registers[0] << 16) | registers[1]

        if tipo == "int32":
            v = (registers[0] << 16) | registers[1]
            return v - 0x100000000 if v & 0x80000000 else v

        if tipo == "float32":
            # 2 registros = 4 bytes = float IEEE-754.
            # Swap de palabras: el PLC entrega la palabra BAJA primero
            # (little-endian por palabra), por eso invertimos el orden.
            packed = struct.pack(">HH", registers[1], registers[0])
            return struct.unpack(">f", packed)[0]

        # Tipo desconocido: devolver crudo
        return registers[0]

    @staticmethod
    def _encode_value(value: Any, tipo: str) -> List[int]:
        """Operación inversa: codifica un valor Python a registros."""
        if tipo == "uint16":
            return [int(value) & 0xFFFF]
        if tipo == "uint32":
            v = int(value) & 0xFFFFFFFF
            return [(v >> 16) & 0xFFFF, v & 0xFFFF]
        if tipo == "float32":
            # Enviamos con la palabra BAJA primero (PLC little-endian por palabra)
            packed = struct.pack(">f", float(value))
            hi, lo = struct.unpack(">HH", packed)
            return [lo, hi]
        return [int(value) & 0xFFFF]

    # ------------------------------------------------------------------ #
    #  Lectura de variables
    # ------------------------------------------------------------------ #
    def read_holding_register(self, reg_config: Dict[str, Any]) -> Optional[Any]:
        """Lee UN holding register según su configuración."""
        if not self.is_connected() and not self.connect():
            return None

        try:
            direccion = reg_config["direccion"]
            tipo = reg_config.get("tipo", "uint16")
            count = 2 if tipo in ("uint32", "int32", "float32") else 1

            rr = self.client.read_holding_registers(
                address=direccion,
                count=count,
                **{_UNIT_KWARG: self.unit_id},
            )
            if rr.isError():
                logger.error(
                    f"[{self.plc_id}] Error leyendo HR {reg_config['nombre']}: {rr}"
                )
                return None

            return self._decode_value(rr.registers, tipo)

        except (ModbusException, ConnectionException) as e:
            logger.error(f"[{self.plc_id}] ModbusException en HR: {e}")
            self._connected = False
            return None
        except Exception as e:
            logger.error(f"[{self.plc_id}] Error inesperado HR: {e}")
            return None

    def write_coil(self, coil_config: Dict[str, Any], value: bool) -> bool:
        """Escribe un coil (booleano) en el PLC."""
        if not self.is_connected() and not self.connect():
            return False

        try:
            direccion = coil_config["direccion"]
            rr = self.client.write_coil(
                address=direccion,
                value=bool(value),
                **{_UNIT_KWARG: self.unit_id},
            )
            if rr.isError():
                logger.error(
                    f"[{self.plc_id}] Error escribiendo coil {coil_config['nombre']}: {rr}"
                )
                return False
            logger.info(
                f"[{self.plc_id}] Coil {coil_config['nombre']} escrito a {bool(value)}"
            )
            return True

        except (ModbusException, ConnectionException) as e:
            logger.error(f"[{self.plc_id}] ModbusException en write_coil: {e}")
            self._connected = False
            return False
        except Exception as e:
            logger.error(f"[{self.plc_id}] Error inesperado write_coil: {e}")
            return False

    def write_holding_register(self, reg_config: Dict[str, Any], value: Any) -> bool:
        """Escribe un holding register (decodifica el valor según su tipo)."""
        if not self.is_connected() and not self.connect():
            return False

        try:
            direccion = reg_config["direccion"]
            tipo = reg_config.get("tipo", "uint16")
            registers = self._encode_value(value, tipo)
            count = len(registers)

            rr = self.client.write_registers(
                address=direccion,
                values=registers,
                **{_UNIT_KWARG: self.unit_id},
            )
            if rr.isError():
                logger.error(
                    f"[{self.plc_id}] Error escribiendo HR {reg_config['nombre']}: {rr}"
                )
                return False
            logger.info(
                f"[{self.plc_id}] HR {reg_config['nombre']} escrito a {value} ({tipo})"
            )
            return True

        except (ModbusException, ConnectionException) as e:
            logger.error(f"[{self.plc_id}] ModbusException en write_HR: {e}")
            self._connected = False
            return False
        except Exception as e:
            logger.error(f"[{self.plc_id}] Error inesperado write_HR: {e}")
            return False

    def read_coil(self, coil_config: Dict[str, Any]) -> Optional[bool]:
        """Lee un coil (booleano)."""
        if not self.is_connected() and not self.connect():
            return None

        try:
            direccion = coil_config["direccion"]
            rr = self.client.read_coils(
                address=direccion,
                count=1,
                **{_UNIT_KWARG: self.unit_id},
            )
            if rr.isError():
                logger.error(
                    f"[{self.plc_id}] Error leyendo coil {coil_config['nombre']}: {rr}"
                )
                return None
            return bool(rr.bits[0])

        except (ModbusException, ConnectionException) as e:
            logger.error(f"[{self.plc_id}] ModbusException en coil: {e}")
            self._connected = False
            return None
        except Exception as e:
            logger.error(f"[{self.plc_id}] Error inesperado coil: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Lectura por nombre (atajo usado por el Agente IA)
    # ------------------------------------------------------------------ #
    def read_variable_by_name(
        self, variable_name: str, registro_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Lee una variable dado el sub-dict de configuración
        (necesario para conocer su tipo y dirección).
        Devuelve un dict listo para enviar a la IA / frontend.
        """
        kind = registro_config.get("kind", "holding_register")

        if kind == "coil":
            value = self.read_coil(registro_config)
        else:
            value = self.read_holding_register(registro_config)

        unidad = registro_config.get("unidad", "")
        if isinstance(value, float):
            formatted = f"{value:.2f} {unidad}".strip()
        elif isinstance(value, bool):
            formatted = "ENCENDIDO" if value else "APAGADO"
        elif value is None:
            formatted = "no disponible"
        else:
            formatted = f"{value} {unidad}".strip()

        return {
            "plc_id": self.plc_id,
            "variable": variable_name,
            "valor": value,
            "valor_legible": formatted,
            "descripcion": registro_config.get("descripcion", ""),
        }

    # ------------------------------------------------------------------ #
    #  Detección de flancos de subida (subida de alarmas)
    # ------------------------------------------------------------------ #
    def detect_rising_edges(
        self, alarmas_config: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Lee todas las coils de alarma configuradas y devuelve una lista de
        aquellas que han hecho FLANCO DE SUBIDA (0 -> 1) desde la última lectura.
        """
        if not alarmas_config:
            return []

        rising_edges = []

        for alarma in alarmas_config:
            coil_cfg = {"direccion": alarma["coil"], "nombre": alarma["nombre"]}
            current = self.read_coil(coil_cfg)
            if current is None:
                continue

            previous = self._last_states.get(alarma["nombre"], False)

            # Flanco de subida detectado
            if current and not previous:
                rising_edges.append(
                    {
                        "plc_id": self.plc_id,
                        "nombre": alarma["nombre"],
                        "mensaje": alarma.get("mensaje", "Alarma sin mensaje"),
                        "severidad": alarma.get("severidad", "media"),
                    }
                )

            self._last_states[alarma["nombre"]] = current

        return rising_edges
