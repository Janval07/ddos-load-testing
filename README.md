# DDoS Load Testing Scripts

Scripts para medir cuánta carga soportan los servicios del dashboard (LDAP/Keycloak, backend y frontend) antes de que Fail2Ban banee al atacante, y para confirmar que las defensas configuradas en cada imagen de Docker funcionan bajo tráfico real.

Ambos scripts se basan en el `flood.py` proporcionado por el profesor (parte del lab `fail2ban-ddos`), pero fueron modificados para:

- Recibir **host y puerto como argumentos de línea de comandos**, en vez de atacar nombres de contenedor fijos (`victim-protected`, `victim-unprotected`).
- Correr **fuera de los 3 contenedores Docker**, directo desde la máquina del atacante.
- Mantener los mismos límites de seguridad del script original (duración, concurrencia y tasa máximas acotadas), para que las pruebas sigan siendo controladas y no una herramienta de ataque real.

## Archivos

| Archivo | Protocolo | Contra qué se usa |
|---|---|---|
| `flood.py` | HTTP (peticiones GET) | Frontend (nginx) y backend (uvicorn/FastAPI) |
| `ldap_flood.py` | TCP puro (conexiones, sin HTTP) | LDAP/LDAPS |

LDAP no habla HTTP, así que `flood.py` no sirve para probarlo: el filtro de Fail2Ban en LDAP (`ldap-flood`) no detecta peticiones GET sino **volumen de conexiones nuevas** (cada conexión entrante genera una línea `ACCEPT` en el log de `slapd`). `ldap_flood.py` reproduce exactamente ese patrón: abre y cierra conexiones TCP puras al puerto que se le indique, sin enviar ningún bind ni búsqueda LDAP.

## Requisitos

- Python 3.8+ instalado en la máquina atacante (no dentro de los contenedores).
- Los 3 servicios corriendo en Docker, cada uno con su jail de Fail2Ban activo.

## Uso

### `flood.py` (HTTP — frontend y backend)

```bash
python flood.py <host> <puerto> [--path /] [--duration 20] [--concurrency 40] [--rps 50]
```

Ejemplos reales usados en esta práctica:

```bash
# Frontend (Vue servido por nginx, puerto 8090)
python flood.py localhost 8090 --duration 15 --concurrency 10 --rps 20

# Backend (FastAPI/uvicorn, puerto 8001)
python flood.py localhost 8001 --duration 15 --concurrency 10 --rps 20
```

Límites máximos aceptados: `--duration` 300s, `--concurrency` 500, `--rps` 500.

### `ldap_flood.py` (TCP — LDAP/LDAPS)

```bash
python ldap_flood.py <host> <puerto> [--duration 20] [--concurrency 40] [--cps 50]
```

Ejemplo real usado en esta práctica:

```bash
# LDAP (OpenLDAP, puerto 389)
python ldap_flood.py localhost 389 --duration 15 --concurrency 10 --cps 20
```

Límites máximos aceptados: `--duration` 300s, `--concurrency` 500, `--cps` (conexiones/segundo) 500.

## Cómo se modificó el script original

El `flood.py` del profesor traía los objetivos (`victim-protected`, `victim-unprotected`) y los parámetros de carga (`DURATION`, `CONCURRENCY`, `REQUESTS_PER_SECOND`) fijos como variables de entorno, pensado para correr dentro de un contenedor `attacker` en la misma red Docker que las víctimas, resolviendo esos nombres por DNS interno.

Cambios realizados:

1. Se reemplazó la lista fija `TARGETS` y las variables de entorno por `argparse`, con `host` y `port` como argumentos posicionales obligatorios y `--duration`, `--concurrency`, `--rps`/`--cps` como opcionales con los mismos valores por defecto y topes máximos del original.
2. Se eliminó la dependencia de la red interna de Docker: el script arma la URL (`http://host:port/path`) o la tupla `(host, port)` directamente desde los argumentos, y corre con cualquier intérprete de Python estándar, sin Docker de por medio.
3. Se creó `ldap_flood.py` como una variante para protocolos no-HTTP: en vez de `urllib.request` con método GET, usa `socket.create_connection()` para abrir y cerrar conexiones TCP puras, que es lo que el filtro `ldap-flood` de Fail2Ban vigila en el log de `slapd`.
4. Se mantuvo intacta la arquitectura de concurrencia con hilos (`ThreadPoolExecutor`) y el limitador de tasa (`RateLimiter`), para que el comportamiento de carga fuera comparable al del script original.

## Resultados de las pruebas de carga

Cada servicio corre dentro de su propio contenedor Docker con Fail2Ban configurado para banear una IP que exceda el umbral dentro de una ventana de tiempo. La configuración de los 3 jails es la misma: **60 eventos (peticiones HTTP o conexiones TCP) en una ventana de 10 segundos** disparan un baneo de 10 minutos (con incremento progresivo en reincidencias, hasta 1 día máximo).

### Frontend (nginx + Vue, puerto 8090) — jail `http-flood`

Prueba: `python flood.py localhost 8090 --duration 15 --concurrency 10 --rps 20`

| Métrica | Valor |
|---|---|
| Peticiones exitosas (200) antes del baneo | 65 |
| Punto de corte | Justo después de cruzar 60 peticiones en la ventana de 10s |
| Resultado tras el baneo | 100% de peticiones fallidas (conexión rechazada/timeout) |

**Carga máxima sostenible sin disparar el baneo:** por debajo de 60 peticiones en 10 segundos, es decir, un ritmo sostenido menor a ~6 peticiones/segundo por IP.

### Backend (FastAPI/uvicorn, puerto 8001) — jail `uvicorn-flood`

Prueba: `python flood.py localhost 8001 --duration 15 --concurrency 10 --rps 20`

El backend no tiene ninguna ruta pública que responda `200` sin JWT (todo está protegido), así que el contador `ok(200)` del script no es indicador aquí; se confirmó el baneo directamente en el log de Fail2Ban:

```
[uvicorn-flood] Ban 172.19.0.1
```

| Métrica | Valor |
|---|---|
| Total de bans acumulados en las pruebas | 2 |
| Umbral de baneo | 60 peticiones en 10s (mismo que el frontend) |

**Carga máxima sostenible sin disparar el baneo:** igual que el frontend, menor a ~6 peticiones/segundo sostenidas por IP.

### LDAP (OpenLDAP + LDAPS, puerto 389/636) — jail `ldap-flood`

Prueba: `python ldap_flood.py localhost 389 --duration 15 --concurrency 10 --cps 20`

| Métrica | Valor |
|---|---|
| Conexiones registradas como "failed" por el filtro antes de banear | 67 |
| Resultado | `Currently banned: 1`, `Total banned: 1`, IP baneada confirmada en `fail2ban-client status ldap-flood` |

**Carga máxima sostenible sin disparar el baneo:** por debajo de 60 conexiones nuevas en 10 segundos.

## Notas de depuración relevantes (jail `ldap-flood`)

A diferencia de nginx y uvicorn, el log nativo de `slapd` (OpenLDAP) no incluye una marca de tiempo que Fail2Ban pueda interpretar — cada línea empieza con un identificador hexadecimal interno (ej. `6acc428b`), no con una fecha. Sin una fecha reconocible, Fail2Ban descarta la línea en silencio aunque el texto coincida con el filtro.

Solución aplicada en `entrypoint.sh` del contenedor LDAP: se envuelve el proceso de `slapd` para anteponer una fecha real (`date -u +%Y-%m-%dT%H:%M:%S.%6NZ`) a cada línea antes de escribirla al log que vigila Fail2Ban. El filtro (`ldap-flood.conf`) se ajustó para no asumir cuántos tokens separan la fecha del resto de la línea, usando `.*` en vez de contar tokens exactos:

```
failregex = ^.*conn=\d+ fd=\d+ ACCEPT from IP=<HOST>:\d+ \(IP=\S+\)
```

## Advertencia

Estos scripts están pensados exclusivamente para probar los 3 servicios de este proyecto, corriendo en `localhost` dentro de un entorno de laboratorio propio. No deben usarse contra ningún host, red o servicio que no sea de tu propiedad.
