"""
ai_context_manager.py
=====================
Clase: AIContextManager

Encargada de cargar, validar y exponer la configuración de los PLCs y sus
registros Modbus. Centraliza el "mapa" del sistema para que el resto de los
componentes (PLC, Agente, WebSocket) consulten una única fuente de verdad.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger("DANTEIndustrial.AIContextManager")


class AIContextManager:
    """
    Gestiona el archivo `config_plcs.json` y expone métodos de consulta
    sobre los PLCs, sus registros y las alarmas configuradas.
    """

    def __init__(self, config_path: str = "config_plcs.json"):
        self.config_path = Path(config_path)
        self.config: Dict[str, Any] = {}
        self.plcs: List[Dict[str, Any]] = []
        self.configuracion_global: Dict[str, Any] = {}
        self._reload()

    # ------------------------------------------------------------------ #
    #  Carga / recarga del JSON de configuración
    # ------------------------------------------------------------------ #
    def _reload(self) -> None:
        """Carga la configuración desde disco. Se puede invocar en caliente."""
        try:
            if not self.config_path.exists():
                raise FileNotFoundError(
                    f"No se encontró el archivo de configuración: {self.config_path}"
                )

            with self.config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            self.config = data
            self.plcs = data.get("plcs", [])
            self.configuracion_global = data.get("configuracion_global", {})

            # Validación mínima de integridad
            for plc in self.plcs:
                if "ip" not in plc or "port" not in plc or "plc_id" not in plc:
                    raise ValueError(
                        f"PLC mal configurado (falta ip/port/plc_id): {plc.get('plc_id')}"
                    )

            logger.info(
                f"Configuración cargada: {len(self.plcs)} PLC(s) registrados."
            )
        except Exception as e:
            logger.error(f"Error cargando configuración: {e}")
            # Dejamos estructuras vacías para no romper el sistema entero
            self.config = {}
            self.plcs = []
            self.configuracion_global = {}

    def reload(self) -> None:
        """Fuerza una recarga del archivo de configuración."""
        self._reload()

    # ------------------------------------------------------------------ #
    #  Métodos de consulta
    # ------------------------------------------------------------------ #
    def get_plc_ids(self) -> List[str]:
        """Retorna los IDs de todos los PLCs configurados."""
        return [plc["plc_id"] for plc in self.plcs]

    def get_plc(self, plc_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene la configuración completa de un PLC por su ID."""
        for plc in self.plcs:
            if plc["plc_id"] == plc_id:
                return plc
        return None

    def get_all_plcs_summary(self) -> List[Dict[str, Any]]:
        """Resumen ligero (sin exponer el detalle de cada registro)."""
        return [
            {
                "plc_id": p["plc_id"],
                "nombre": p.get("nombre", ""),
                "ip": p.get("ip"),
                "port": p.get("port"),
                "descripcion": p.get("descripcion", ""),
            }
            for p in self.plcs
        ]

    def find_register_by_name(self, variable_name: str) -> Optional[Dict[str, Any]]:
        """
        Busca una variable (holding register o coil) por nombre en TODOS los PLCs.
        Retorna el primer match junto con el PLC al que pertenece.
        """
        nombre_l = variable_name.lower()
        for plc in self.plcs:
            for hr in plc.get("registros", {}).get("holding_registers", []):
                if hr["nombre"].lower() == nombre_l:
                    return {"plc_id": plc["plc_id"], "kind": "holding_register", **hr}
            for c in plc.get("registros", {}).get("coils", []):
                if c["nombre"].lower() == nombre_l:
                    return {"plc_id": plc["plc_id"], "kind": "coil", **c}
        return None

    def find_writable_by_name(self, variable_name: str) -> Optional[Dict[str, Any]]:
        """
        Busca una variable ESCRIBIBLE (setpoint o comando) por nombre.
        Retorna dict con 'plc_id', 'kind' ('holding_register'|'coil') y resto de campos.
        """
        nombre_l = variable_name.lower()
        for plc in self.plcs:
            esc = plc.get("registros", {}).get("escribibles", {})
            for hr in esc.get("holding_registers", []):
                if hr["nombre"].lower() == nombre_l:
                    return {"plc_id": plc["plc_id"], "kind": "holding_register", **hr}
            for c in esc.get("coils", []):
                if c["nombre"].lower() == nombre_l:
                    return {"plc_id": plc["plc_id"], "kind": "coil", **c}
        return None

    def list_writable_variables(self) -> List[Dict[str, Any]]:
        """Lista todas las variables escribibles del sistema, con su PLC."""
        out: List[Dict[str, Any]] = []
        for plc in self.plcs:
            esc = plc.get("registros", {}).get("escribibles", {})
            for hr in esc.get("holding_registers", []):
                out.append({"plc_id": plc["plc_id"], "kind": "holding_register", **hr})
            for c in esc.get("coils", []):
                out.append({"plc_id": plc["plc_id"], "kind": "coil", **c})
        return out

    def validate_writable_value(self, variable_cfg: Dict[str, Any], value: Any) -> Optional[str]:
        """Valida que `value` sea aceptable para `variable_cfg`. Devuelve mensaje de error o None."""
        rmin = variable_cfg.get("rango_min")
        rmax = variable_cfg.get("rango_max")
        if rmin is not None and rmax is not None:
            try:
                v_num = float(value)
                if v_num < rmin or v_num > rmax:
                    return f"Valor fuera de rango [{rmin}..{rmax}]"
            except (TypeError, ValueError):
                return "El valor debe ser numérico"
        kind = variable_cfg.get("kind")
        if kind == "coil":
            if not isinstance(value, bool):
                return "Para un coil/ON-OFF el valor debe ser booleano (true/false)"
        return None

    def get_alarms_for_plc(self, plc_id: str) -> List[Dict[str, Any]]:
        """Devuelve las alarmas configuradas para un PLC concreto."""
        plc = self.get_plc(plc_id)
        if not plc:
            return []
        return plc.get("alarmas", [])

    def get_global_config(self) -> Dict[str, Any]:
        """Configuración global: intervalos, proveedor de IA, etc."""
        return self.configuracion_global

    def build_context_for_ai(self) -> str:
        """
        Construye un resumen textual del sistema, útil como contexto para
        el prompt del agente IA.
        """
        lines = ["PLCs disponibles en la planta:"]
        for plc in self.plcs:
            lines.append(f"- {plc['plc_id']} ({plc.get('nombre','')}): {plc['ip']}:{plc['port']}")
            for hr in plc.get("registros", {}).get("holding_registers", []):
                unidad = hr.get("unidad", "")
                lines.append(
                    f"    * {hr['nombre']} ({hr['tipo']}) -> {unidad}"
                )
            for c in plc.get("registros", {}).get("coils", []):
                lines.append(f"    * {c['nombre']} (coil)")
        return "\n".join(lines)
