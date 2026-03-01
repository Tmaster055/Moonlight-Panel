from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import subprocess
import os
import shutil
import yaml
import json
import psutil
import shlex
from dotenv import load_dotenv
from functools import wraps
from datetime import datetime
from werkzeug.utils import secure_filename
from cloudflare_helper import create_srv_record, delete_srv_record

load_dotenv()
app = Flask(__name__)
app.secret_key = "ein_geheimer_schluessel"

BASE_DIR = os.path.abspath("./servers")
if not os.path.exists(BASE_DIR):
    os.makedirs(BASE_DIR)

def cleanup_legacy_server_info():
    """Remove legacy metadata files now that compose is the source of truth."""
    for name in os.listdir(BASE_DIR):
        server_dir = os.path.join(BASE_DIR, name)
        if not os.path.isdir(server_dir):
            continue
        legacy_info = os.path.join(server_dir, "server.info")
        if os.path.exists(legacy_info):
            try:
                os.remove(legacy_info)
            except OSError:
                pass

cleanup_legacy_server_info()

# --- Simple user store (file-based). For demo only. Use hashed passwords and a DB in production.
USERS_FILE = os.path.abspath("users.json")

def ensure_default_user():
    if not os.path.exists(USERS_FILE):
        default = {"admin": {"password": "admin", "display": "Administrator"}}
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)

def load_users():
    ensure_default_user()
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def verify_user(username, password):
    users = load_users()
    user = users.get(username)
    if not user:
        return False
    return user.get("password") == password

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

# --- Backend Logik ---
def create_compose_yaml(name, difficulty, server_type, ports, version, bedrock=False,
                        max_players=20, enable_whitelist=True, enable_pvp=True, allow_flight=False,
                        view_distance=20, memory=8192, motd=None, seed=None,
                        spawn_monsters=True, spawn_animals=True):
    port_list = [p.strip() for p in ports.split(",") if p.strip()]
    if not port_list:
        port_list = ["25565:25565"]
    if not any(p.startswith("25575:") for p in port_list):
        port_list.append("25575:25575")

    server_port = port_list[0].split(":")[0]

    environment = {
        "EULA": "TRUE",
        "SERVER_PORT": server_port,
        "RCON_CMDS_STARTUP": "gamerule players_sleeping_percentage 0",
        "VIEW_DISTANCE": str(view_distance),
        "MOTD": motd or os.environ.get("SERVER_MOTD"),
        "ICON": os.environ.get("SERVER_ICON"),
        "ENABLE_WHITELIST": "true" if enable_whitelist else "false",
        "TYPE": server_type,
        "VERSION": version,
        "MEMORY": f"{memory}M",
        "TZ": "Europe/Vienna",
        "DIFFICULTY": difficulty,
        "ENABLE_RCON": "true",
        "RCON_PASSWORD": "123",
        "RCON_PORT": "25575",
        "MAX_PLAYERS": str(max_players),
        "PVP": "true" if enable_pvp else "false",
        "ALLOW_FLIGHT": "true" if allow_flight else "false",
        "SPAWN_MONSTERS": "true" if spawn_monsters else "false",
        "SPAWN_ANIMALS": "true" if spawn_animals else "false"
    }
    if seed is not None and str(seed).strip():
        environment["SEED"] = str(seed)

    # Always use OPS from .env if available
    ops_list = os.environ.get("OPS")
    if ops_list:
        environment["OPS"] = ops_list

    if bedrock and server_type.upper() in ("PAPER", "FOLIA"):
        environment["PLUGINS"] = (
            "https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/spigot\n"
            "https://download.geysermc.org/v2/projects/floodgate/versions/latest/builds/latest/downloads/spigot"
        )

    compose_dict = {
        "version": "3.8",
        "services": {
            "mc": {
                "image": "itzg/minecraft-server:latest",
                "container_name": f"mc_{name}",
                "tty": True,
                "stdin_open": True,
                "restart": "unless-stopped",
                "ports": port_list,
                "environment": environment,
                "volumes": [f"./data/{name}:/data"]
            },
            "backups": {
                "image": "itzg/mc-backup",
                "depends_on": ["mc"],
                "environment": {
                    "BACKUP_INTERVAL": "48h",
                    "BACKUP_RETENTION_DAYS": "6",
                    "RCON_HOST": "mc",
                    "RCON_PORT": "25575",
                    "RCON_PASSWORD": "123",
                    "PAUSE_IF_PLAYERS_ONLINE": "true",
                    "INITIAL_DELAY": 0
                },
                "volumes": [
                    f"./data/{name}:/data:ro",
                    "./data/mc-backups:/backups"
                ]
            }
        }
    }

    return compose_dict


def run_compose(name, command):
    server_dir = os.path.join(BASE_DIR, name)
    compose_file = os.path.join(server_dir, "docker-compose.yml")
    if not os.path.exists(compose_file):
        raise Exception(f"Server {name} existiert nicht.")
    subprocess.run(["docker", "compose", "-f", compose_file] + command, check=True, cwd=server_dir)

def server_exists(name):
    return os.path.exists(os.path.join(BASE_DIR, name, "docker-compose.yml"))

def _server_data_dir(name):
    return os.path.join(BASE_DIR, name, "data", name)

def _normalize_relative_path(path_value):
    return (path_value or "").replace("\\", "/").strip("/")

def _resolve_server_data_path(name, relative_path="", must_exist=False):
    base = os.path.abspath(_server_data_dir(name))
    relative = _normalize_relative_path(relative_path)
    candidate = os.path.abspath(os.path.join(base, relative)) if relative else base
    if os.path.commonpath([base, candidate]) != base:
        raise ValueError("Ungultiger Dateipfad.")
    if must_exist and not os.path.exists(candidate):
        raise FileNotFoundError("Datei oder Ordner nicht gefunden.")
    return candidate

def _relative_to_server_data(name, absolute_path):
    base = os.path.abspath(_server_data_dir(name))
    rel = os.path.relpath(absolute_path, base)
    return "" if rel == "." else rel.replace("\\", "/")

def _format_size(size):
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"

def _ensure_docker_user_ownership(name, relative_path=""):
    """Best effort: set owner/group to the default mc container user."""
    server_dir = os.path.join(BASE_DIR, name)
    compose_file = os.path.join(server_dir, "docker-compose.yml")
    if not os.path.exists(compose_file):
        return None

    rel = _normalize_relative_path(relative_path)
    target = "/data" if not rel else f"/data/{rel}"
    ownership_cmd = f'uid=$(id -u); gid=$(id -g); chown -R "$uid:$gid" {shlex.quote(target)}'

    try:
        subprocess.run(
            [
                "docker", "compose", "-f", compose_file,
                "run", "--rm", "--no-deps", "-T", "mc",
                "sh", "-lc", ownership_cmd,
            ],
            check=True,
            cwd=server_dir,
            capture_output=True,
            text=True,
        )
        return None
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        return stderr or str(e)


def _normalize_environment(environment):
    """Normalize Compose environment to a dict for consistent key lookups."""
    if isinstance(environment, dict):
        return {str(k): str(v) for k, v in environment.items()}
    if isinstance(environment, list):
        env = {}
        for item in environment:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                env[k] = v
        return env
    return {}


def get_server_info(name):
    compose_path = os.path.join(BASE_DIR, name, "docker-compose.yml")
    if not os.path.exists(compose_path):
        return None
    try:
        with open(compose_path, "r", encoding="utf-8") as f:
            compose = yaml.safe_load(f) or {}
    except Exception:
        return None

    services = compose.get("services", {}) if isinstance(compose, dict) else {}
    mc = services.get("mc", {}) if isinstance(services, dict) else {}
    if not isinstance(mc, dict):
        return None

    environment = _normalize_environment(mc.get("environment", {}))
    port_mappings = [str(p).strip() for p in mc.get("ports", []) if str(p).strip()]
    ports = ",".join(port_mappings)

    has_bedrock_port = any("/udp" in p.lower() and "19132:19132" in p.replace(" ", "") for p in port_mappings)
    bedrock = has_bedrock_port or bool(environment.get("PLUGINS"))

    return {
        "difficulty": environment.get("DIFFICULTY", "?"),
        "server_type": environment.get("TYPE", "?"),
        "ports": ports,
        "port_mappings": port_mappings,
        "mc_port": _extract_mc_port(port_mappings),
        "version": environment.get("VERSION", "LATEST"),
        "bedrock": bedrock,
    }

def _extract_host_port(mapping):
    """Extract host port from mappings like '25565:25565' or '25565:25565/udp'."""
    if not mapping:
        return None
    first = mapping.split(":", 1)[0].strip().strip('"')
    try:
        return int(first)
    except ValueError:
        return None

def _parse_port_mappings(ports):
    """Parse stored port mappings from compose-derived values into a clean list."""
    if not ports:
        return []
    if isinstance(ports, list):
        return [str(p).strip() for p in ports if str(p).strip()]
    return [p.strip() for p in str(ports).split(",") if p.strip()]

def _extract_mc_port(port_mappings):
    """Return first TCP Minecraft host port, excluding RCON and Bedrock ports."""
    for mapping in _parse_port_mappings(port_mappings):
        if "/udp" in mapping.lower():
            continue
        port = _extract_host_port(mapping)
        if port is None:
            continue
        if port in (25575, 19132):
            continue
        return port
    return None

def used_mc_ports():
    """Return used Minecraft host ports, excluding reserved RCON/Bedrock ports."""
    used = set()
    for server in list_servers():
        for mapping in _parse_port_mappings(server.get("ports", "")):
            # Skip bedrock mappings and any UDP mapping
            if "/udp" in mapping.lower():
                continue
            port = _extract_host_port(mapping)
            if port is None:
                continue
            # Ignore reserved non-MC ports
            if port in (25575, 19132):
                continue
            used.add(port)
    return used

def next_available_mc_port(start=25565):
    used = used_mc_ports()
    port = start
    while port <= 65535:
        if port not in used:
            return port
        port += 1
    return None

def is_running(name):
    c = f"mc_{name}"
    result = subprocess.run(["docker", "ps", "--filter", f"name=^{c}$", "--format", "{{.Names}}"], capture_output=True, text=True)
    return bool(result.stdout.strip())

def get_server_logs(name, tail=250):
    server_dir = os.path.join(BASE_DIR, name)
    compose_file = os.path.join(server_dir, "docker-compose.yml")
    if not os.path.exists(compose_file):
        raise FileNotFoundError(f"Server '{name}' existiert nicht.")

    # Read-only access to current/past logs of the Minecraft service.
    cmd = [
        "docker", "compose", "-f", compose_file,
        "logs", "--no-color", "--tail", str(tail), "mc"
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=server_dir,
        timeout=15,
    )
    if result.returncode != 0:
        error_msg = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(error_msg or "Logs konnten nicht gelesen werden.")
    return (result.stdout or "").rstrip()

@app.route('/system-stats')
@login_required
def system_stats():
    """Return host utilization (per-core CPU, RAM, and storage)."""
    try:
        # Use one sampled read and derive total from per-core values to avoid stale 0% readings.
        cpu_per_core_raw = psutil.cpu_percent(interval=0.2, percpu=True)
        cpu_per_core = [round(float(v), 1) for v in cpu_per_core_raw] if cpu_per_core_raw else []
        cpu_total = round((sum(cpu_per_core) / len(cpu_per_core)), 1) if cpu_per_core else 0.0

        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.abspath(os.sep))

        return jsonify({
            "cpu_total_pct": cpu_total,
            "cpu_per_core_pct": cpu_per_core,
            "memory": {
                "total": mem.total,
                "used": mem.used,
                "available": mem.available,
                "percent": round(mem.percent, 1),
            },
            "storage": {
                "path": os.path.abspath(os.sep),
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "percent": round(disk.percent, 1),
            },
        })
    except Exception as e:
        return jsonify({"error": "system_stats_failed", "message": str(e)}), 500

def list_servers():
    servers = []
    for name in os.listdir(BASE_DIR):
        info = get_server_info(name)
        if info:
            bedrock_value = info.get("bedrock", False)
            if isinstance(bedrock_value, str):
                bedrock_value = bedrock_value.lower() == "true"
            servers.append({
                "name": name,
                "difficulty": info.get("difficulty", "?"),
                "server_type": info.get("server_type", "?"),
                "ports": info.get("ports", "?"),
                "mc_port": info.get("mc_port"),
                "version": info.get("version", "LATEST"),
                "bedrock": bool(bedrock_value),
                "running": is_running(name),
            })
    return servers

@app.route("/")
@login_required
def index():
    return render_template('index.html', servers=list_servers(), message=request.args.get('message'), user=session.get('user'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')
    username = request.form.get('username')
    password = request.form.get('password')
    if verify_user(username, password):
        session['user'] = username
        next_url = request.args.get('next') or url_for('index')
        return redirect(next_url)
    return render_template('login.html', error='Ungültiger Benutzername oder Passwort')


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/server/<name>/files')
@login_required
def server_files(name):
    if not server_exists(name):
        return redirect(url_for("index", message=f"Server '{name}' existiert nicht."))

    os.makedirs(_server_data_dir(name), exist_ok=True)
    message = request.args.get("message")
    rel_path = _normalize_relative_path(request.args.get("path", ""))

    try:
        current_dir = _resolve_server_data_path(name, rel_path, must_exist=True)
        if not os.path.isdir(current_dir):
            return redirect(url_for("server_files", name=name, message="Gewahlter Pfad ist kein Ordner."))
    except Exception as e:
        return redirect(url_for("server_files", name=name, message=str(e)))

    entries = []
    for item in os.scandir(current_dir):
        item_rel = _relative_to_server_data(name, item.path)
        stat = item.stat()
        entries.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "size": _format_size(stat.st_size) if item.is_file() else "-",
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "path": item_rel,
        })

    entries.sort(key=lambda row: (not row["is_dir"], row["name"].lower()))
    parent_path = _normalize_relative_path(os.path.dirname(rel_path)) if rel_path else None

    return render_template(
        "file_manager.html",
        server_name=name,
        entries=entries,
        current_path=rel_path,
        parent_path=parent_path,
        message=message,
        edit_mode=False,
    )

@app.route('/server/<name>/console')
@login_required
def server_console(name):
    if not server_exists(name):
        return redirect(url_for("index", message=f"Server '{name}' existiert nicht."))

    logs = ""
    error = None
    try:
        logs = get_server_logs(name)
    except Exception as e:
        error = str(e)

    return render_template(
        "server_console.html",
        server_name=name,
        initial_logs=logs,
        error=error,
    )

@app.route('/server/<name>/logs')
@login_required
def server_logs(name):
    if not server_exists(name):
        return jsonify({"error": "server_not_found", "message": f"Server '{name}' existiert nicht."}), 404
    try:
        logs = get_server_logs(name)
        return jsonify({"logs": logs})
    except Exception as e:
        return jsonify({"error": "logs_failed", "message": str(e)}), 500

@app.route('/server/<name>/files/upload', methods=['POST'])
@login_required
def server_files_upload(name):
    current_path = _normalize_relative_path(request.form.get("path", ""))
    file_obj = request.files.get("file")

    if not server_exists(name):
        return redirect(url_for("index", message=f"Server '{name}' existiert nicht."))
    if not file_obj or not file_obj.filename:
        return redirect(url_for("server_files", name=name, path=current_path, message="Bitte eine Datei auswahlen."))

    filename = secure_filename(file_obj.filename)
    if not filename:
        return redirect(url_for("server_files", name=name, path=current_path, message="Ungultiger Dateiname."))

    try:
        target_dir = _resolve_server_data_path(name, current_path, must_exist=True)
        if not os.path.isdir(target_dir):
            raise ValueError("Zielpfad ist kein Ordner.")
        destination = os.path.join(target_dir, filename)
        file_obj.save(destination)
        ownership_error = _ensure_docker_user_ownership(name, _relative_to_server_data(name, destination))
        msg = f"Datei '{filename}' wurde hochgeladen."
        if ownership_error:
            msg += f" Hinweis bei Rechte-Update: {ownership_error}"
        return redirect(url_for("server_files", name=name, path=current_path, message=msg))
    except Exception as e:
        return redirect(url_for("server_files", name=name, path=current_path, message=f"Upload-Fehler: {e}"))

@app.route('/server/<name>/files/delete', methods=['POST'])
@login_required
def server_files_delete(name):
    current_path = _normalize_relative_path(request.form.get("path", ""))
    target_rel = _normalize_relative_path(request.form.get("target", ""))

    if not server_exists(name):
        return redirect(url_for("index", message=f"Server '{name}' existiert nicht."))
    if not target_rel:
        return redirect(url_for("server_files", name=name, path=current_path, message="Kein Ziel zum Loschen angegeben."))

    try:
        target_abs = _resolve_server_data_path(name, target_rel, must_exist=True)
        if os.path.isdir(target_abs):
            shutil.rmtree(target_abs)
        else:
            os.remove(target_abs)

        parent_rel = _normalize_relative_path(os.path.dirname(target_rel))
        ownership_error = _ensure_docker_user_ownership(name, parent_rel)
        msg = f"'{os.path.basename(target_abs)}' wurde geloscht."
        if ownership_error:
            msg += f" Hinweis bei Rechte-Update: {ownership_error}"
        return redirect(url_for("server_files", name=name, path=current_path, message=msg))
    except Exception as e:
        return redirect(url_for("server_files", name=name, path=current_path, message=f"Losch-Fehler: {e}"))

@app.route('/server/<name>/files/edit', methods=['GET', 'POST'])
@login_required
def server_files_edit(name):
    file_path = _normalize_relative_path(request.values.get("file", ""))

    if not server_exists(name):
        return redirect(url_for("index", message=f"Server '{name}' existiert nicht."))
    if not file_path:
        return redirect(url_for("server_files", name=name, message="Keine Datei zum Bearbeiten ausgewahlt."))

    try:
        file_abs = _resolve_server_data_path(name, file_path, must_exist=True)
        if os.path.isdir(file_abs):
            return redirect(url_for("server_files", name=name, message="Ordner konnen nicht direkt bearbeitet werden."))
    except Exception as e:
        return redirect(url_for("server_files", name=name, message=f"Fehler beim Offnen: {e}"))

    parent_path = _normalize_relative_path(os.path.dirname(file_path))

    if request.method == "POST":
        content = request.form.get("content", "")
        try:
            with open(file_abs, "w", encoding="utf-8") as f:
                f.write(content)
            ownership_error = _ensure_docker_user_ownership(name, file_path)
            msg = f"Datei '{os.path.basename(file_path)}' wurde gespeichert."
            if ownership_error:
                msg += f" Hinweis bei Rechte-Update: {ownership_error}"
            return redirect(url_for("server_files", name=name, path=parent_path, message=msg))
        except Exception as e:
            return render_template(
                "file_manager.html",
                server_name=name,
                entries=[],
                current_path=parent_path,
                parent_path=_normalize_relative_path(os.path.dirname(parent_path)) if parent_path else None,
                message=f"Speicher-Fehler: {e}",
                edit_mode=True,
                edit_file=file_path,
                edit_content=content,
            )

    try:
        with open(file_abs, "r", encoding="utf-8") as f:
            file_content = f.read()
    except UnicodeDecodeError:
        return redirect(url_for("server_files", name=name, path=parent_path, message="Datei ist nicht UTF-8 und kann nicht im Editor angezeigt werden."))
    except Exception as e:
        return redirect(url_for("server_files", name=name, path=parent_path, message=f"Fehler beim Lesen: {e}"))

    return render_template(
        "file_manager.html",
        server_name=name,
        entries=[],
        current_path=parent_path,
        parent_path=_normalize_relative_path(os.path.dirname(parent_path)) if parent_path else None,
        message=request.args.get("message"),
        edit_mode=True,
        edit_file=file_path,
        edit_content=file_content,
    )

@app.route("/create", methods=["POST"])
@login_required
def create_server():
    name = request.form["name"]
    difficulty = request.form["difficulty"]
    server_type = request.form["server_type"]
    version = request.form["version"]
    raw_port = request.form.get("port", "25565").strip()
    auto_port = request.form.get("auto_port") == "on"
    bedrock = request.form.get('bedrock') == 'true'
    max_players = int(request.form.get("max_players", 20))
    enable_whitelist = request.form.get('enable_whitelist') == 'on'
    enable_pvp = request.form.get('enable_pvp') == 'on'
    allow_flight = request.form.get('allow_flight') == 'on'
    view_distance = int(request.form.get("view_distance", 20))
    memory = int(request.form.get("memory", 8192))
    motd = request.form.get("motd", "")
    seed = request.form.get("seed", "").strip()
    spawn_monsters = request.form.get('spawn_monsters') == 'on'
    spawn_animals = request.form.get('spawn_animals') == 'on'

    # Prüfen, ob schon ein Server läuft
    running_servers = [s for s in list_servers() if s["running"]]
    if running_servers:
        return redirect(url_for("index", message=f"❌ Es läuft bereits der Server '{running_servers[0]['name']}'. Es kann immer nur ein Server gleichzeitig laufen."))

    if server_exists(name):
        return redirect(url_for("index", message=f"Server '{name}' existiert bereits."))

    if auto_port:
        chosen_mc_port = next_available_mc_port(start=25565)
        if chosen_mc_port is None:
            return redirect(url_for("index", message="Fehler: Kein freier Minecraft-Port zwischen 25565 und 65535 gefunden."))
    else:
        try:
            chosen_mc_port = int(raw_port)
        except ValueError:
            return redirect(url_for("index", message="Fehler: UngÃ¼ltiger Port. Bitte gib eine Zahl ein."))
        if chosen_mc_port < 1024 or chosen_mc_port > 65535:
            return redirect(url_for("index", message="Fehler: Port muss zwischen 1024 und 65535 liegen."))
        if chosen_mc_port in used_mc_ports():
            return redirect(url_for("index", message=f"Fehler: Port {chosen_mc_port} wird bereits von einem anderen Server verwendet."))

    mapped_ports = [f"{chosen_mc_port}:{chosen_mc_port}"]
    # If bedrock support is requested for PAPER/FOLIA, add the UDP port mapping for Bedrock (19132)
    if bedrock and server_type in ('PAPER', 'FOLIA'):
        mapped_ports.append('19132:19132/udp')
    mapped_ports.append('25575:25575')
    ports = ",".join(mapped_ports)

    # Determine SRV port: choose the first TCP host port mapping (skip /udp mappings)
    srv_port = None
    for mp in mapped_ports:
        if '/udp' in mp.lower():
            continue
        # mp is expected in form 'host:container' or 'host:container/proto'
        host = mp.split(':', 1)[0]
        host = host.strip().strip('"')
        if host:
            try:
                srv_port = int(host)
                break
            except ValueError:
                continue
    if srv_port is None:
        srv_port = chosen_mc_port

    server_dir = os.path.join(BASE_DIR, name)
    os.makedirs(server_dir, exist_ok=True)

    with open(os.path.join(server_dir, "docker-compose.yml"), "w") as f:
        yaml.dump(create_compose_yaml(name, difficulty, server_type, ports, version, bedrock=bedrock,
                                      max_players=max_players, enable_whitelist=enable_whitelist,
                                      enable_pvp=enable_pvp, allow_flight=allow_flight,
                                      view_distance=view_distance, memory=memory, motd=motd, seed=seed,
                                      spawn_monsters=spawn_monsters, spawn_animals=spawn_animals), f, sort_keys=False)

    # Cleanup legacy metadata file if present; compose is now the source of truth.
    legacy_info = os.path.join(server_dir, "server.info")
    if os.path.exists(legacy_info):
        try:
            os.remove(legacy_info)
        except OSError:
            pass

    try:
        run_compose(name, ["up", "-d"])
        # Try to create the SRV record in Cloudflare. This is best-effort and will not block the flow on failure.
        try:
            create_srv_record(name, port=srv_port)
        except Exception as e:
            # don't fail the request if DNS update fails; log to console
            print(f"Warning: failed to create SRV record for {name}: {e}")
        msg = f"Server '{name}' wurde erstellt und gestartet."
    except subprocess.CalledProcessError as e:
        msg = f"Fehler beim Start von '{name}': {e}"

    return redirect(url_for("index", message=msg))

@app.route("/action", methods=["POST"])
@login_required
def server_action():
    name = request.form["name"]
    action = request.form["action"]
    try:
        if action == "start" or action == "restart":
            # Prüfen, ob bereits ein anderer Server läuft
            running_servers = [s for s in list_servers() if s["running"] and s["name"] != name]
            if running_servers:
                return redirect(url_for("index", message=f"❌ Es läuft bereits der Server '{running_servers[0]['name']}'. Es kann immer nur ein Server gleichzeitig laufen."))

            if action == "start":
                run_compose(name, ["up", "-d"])
            else:
                run_compose(name, ["restart"])

        elif action == "stop":
            run_compose(name, ["stop"])
        elif action == "delete":
            run_compose(name, ["down", "-v"])
            # Try to delete SRV record(s) for this server before removing files
            try:
                delete_srv_record(name)
            except Exception as e:
                print(f"Warning: failed to delete SRV record for {name}: {e}")
            shutil.rmtree(os.path.join(BASE_DIR, name), ignore_errors=True)
        else:
            raise Exception("Unbekannte Aktion.")

        message = f"Aktion '{action}' für Server '{name}' ausgeführt."
    except Exception as e:
        message = f"Fehler: {e}"

    return redirect(url_for("index", message=message))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
