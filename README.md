# DANTE Industrial

Puente web inteligente entre un PLC tradicional (Modbus TCP) y un
asistente de voz con IA. Permite **consultar el estado de la planta por
voz** y **recibir alertas proactivas** en una consola web.

```
   ┌──────────────┐   WebSocket    ┌────────────────┐   Modbus TCP   ┌──────┐
   │  Navegador   │ <─────────────▶│  FastAPI       │ <────────────▶ │ PLC  │
   │  (Web Speech │   voz/texto    │  DANTE         │   polling +    │      │
   │   STT + TTS) │ ──────────────▶│  Industrial    │   flancos      │      │
   └──────────────┘                └────────────────┘                └──────┘
```

## Estructura del proyecto

```
dante_industrial/
├── main.py                      # Servidor FastAPI + lifespan
├── ai_context_manager.py        # Carga y consulta config_plcs.json
├── plc_manager.py               # Cliente Modbus TCP (pymodbus)
├── webhook_bridge.py            # Worker async: sondeo de alarmas
├── websocket_manager.py         # Clientes WS activos
├── industrial_agent_service.py  # Lógica IA (local / openai / anthropic)
├── config_plcs.json             # PLCs + registros + alarmas
├── index.html                   # Frontend oscuro industrial
├── requirements.txt
└── README.md
```

## Puesta en marcha

### ⚠️ Importante: error `externally-managed-environment`

En **Debian 12+ / Ubuntu 23.04+ / Linux Mint / Raspberry Pi OS** y en algunas
distribuciones recientes, pip está bloqueado por PEP 668 y devuelve:

```
error: externally-managed-environment
× This environment is externally managed
```

**Solución recomendada:** usar un entorno virtual (no romper el sistema).

```bash
# 1. Crear y activar un entorno virtual
python3 -m venv .venv
source .venv/bin/activate           # Linux/macOS
# .venv\Scripts\activate            # Windows PowerShell

# 2. Actualizar pip dentro del venv
python -m pip install --upgrade pip

# 3. Instalar dependencias
python -m pip install -r requirements.txt
```

**Alternativas** (sólo si sabes lo que haces):

```bash
# A) Instalar SISTEMA-wide (rompe paquetes gestionados por apt)
pip install --break-system-packages -r requirements.txt

# B) Usar pipx (sólo si empaquetas esto como aplicación CLI)
pipx install --system-site-packages -r requirements.txt
```

**Windows:** `python -m venv .venv` + `.\.venv\Scripts\Activate.ps1` y
después `pip install -r requirements.txt`. No se ve este error.

Verifica que estás en el venv: tu prompt debería mostrar `(.venv)`.

---

### 1. Instalar dependencias

```bash
# DENTRO del entorno virtual, ya activado:
python -m pip install -r requirements.txt
```

### 2. (Opcional) Ajustar `config_plcs.json`

Por defecto apunta a dos PLCs de ejemplo (`192.168.1.10` y `192.168.1.11`).
Cambia las IPs y registros según tu instalación real.

Si no tienes un PLC físico, el sistema **no se rompe**: simplemente los
`PLCManager` no podrán conectar y el agente seguirá respondiendo con
"variable no disponible". El servidor es totalmente funcional incluso sin
PLC conectado.

### 3. Arrancar el servidor

```bash
python main.py
```

o, equivalente:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Variables de entorno útiles:

| Variable          | Por defecto       | Descripción                          |
|-------------------|-------------------|--------------------------------------|
| `DANTE_HOST`     | `0.0.0.0`         | Host de escucha                      |
| `DANTE_PORT`     | `8000`            | Puerto HTTP                          |
| `DANTE_CONFIG`   | `config_plcs.json`| Ruta del JSON de configuración       |
| `DANTE_LOG_LEVEL`| `INFO`            | Nivel de logging                     |
| `DANTE_CORS_ORIGINS` | (vacío)       | Orígenes permitidos, separados por comas |

> También se siguen leyendo las antiguas `ODIN_*` si las `DANTE_*` no
> están definidas.

### 4. Abrir la consola web

Navegador → `http://localhost:8000`

- Pulsa el **botón central** y formula tu consulta por voz.
- También puedes escribirla en el cuadro de texto y pulsar **Enviar**.
- Las alarmas se mostrarán a la derecha y se anunciarán por voz
  automáticamente.
- Toggle **TTS · ON/OFF** arriba a la derecha del panel central para
  silenciar la síntesis de voz.

## Endpoints disponibles

| Método | Ruta                  | Descripción                                         |
|--------|-----------------------|-----------------------------------------------------|
| `GET`  | `/`                   | Sirve `index.html` (consola web).                   |
| `GET`  | `/api/health`         | Estado del servidor y de los PLCs.                  |
| `GET`  | `/api/plcs`           | Lista de PLCs configurados.                         |
| `POST` | `/api/voice-query`    | Procesa una consulta de voz. Devuelve texto + datos. |
| `POST` | `/api/webhook-receiver`| Webhook genérico para inyectar eventos externos.    |
| `WS`   | `/ws`                 | Canal tiempo-real con la UI.                        |

### Ejemplos rápidos

```bash
# Consulta
curl -X POST http://localhost:8000/api/voice-query \
  -H "Content-Type: application/json" \
  -d '{"query":"¿cuál es la temperatura del motor?"}'
```

```bash
# Webhook externo (inyectar una alarma)
curl -X POST http://localhost:8000/api/webhook-receiver \
  -H "Content-Type: application/json" \
  -d '{"tipo":"alarma","mensaje":"Nivel de aceite bajo en PLC_03","severidad":"alta","origen":"MES"}'
```

```bash
# Health
curl http://localhost:8000/api/health
```

## Cambiar el "cerebro" de la IA

En `config_plcs.json` configura:

```json
"configuracion_global": {
  "ia_provider": "local",
  "ia_model": "DANTE-engine-v1"
}
```

Valores válidos de `ia_provider`:

- **`local`** (por defecto): reglas + lectura real del PLC, sin coste y sin API key.
- **`openai`**: usa la API de OpenAI. Añade `openai` a `requirements.txt`,
  pon `OPENAI_API_KEY` en tu entorno y define `openai_api_key` en
  `configuracion_global`.
- **`anthropic`**: usa la API de Anthropic Claude. Igual con `anthropic`
  + `ANTHROPIC_API_KEY` + `anthropic_api_key` en el JSON.

## Detección de flancos de subida

`WebhookBridge._poll_plc` hace polling a cada PLC con la cadencia definida
en `poll_interval_ms`. Para cada alarma configurada compara el estado
actual del coil con el almacenado en `_last_states`. Cuando detecta `0 → 1`
lo difunde inmediatamente por WebSocket y la UI lo anuncia por voz.

## Notas de operación

- **Sin Chrome/Edge**, `SpeechRecognition` puede no existir → se muestra
  un banner y se puede usar el input de texto igualmente.
- Para voces TTS en español puede ser necesario instalar el paquete de
  voces del sistema operativo.
- En producción, restringir `allow_origins` en `CORSMiddleware` y servir
  por HTTPS/WSS.

---

Listo. Copia los archivos en una carpeta, instala dependencias y arranca
`python main.py`. El sistema estará operativo de inmediato.
