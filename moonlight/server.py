from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import subprocess
import os
import shutil
import re
import json
from dotenv import load_dotenv
from functools import wraps
from cloudflare_helper import create_srv_record, delete_srv_record

load_dotenv()
app = Flask(__name__)
app.secret_key = "ein_geheimer_schluessel"

BASE_DIR = os.path.abspath("./servers")
if not os.path.exists(BASE_DIR):
    os.makedirs(BASE_DIR)

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
def create_compose_yaml(name, difficulty, server_type, ports, version, bedrock=False):
    port_list = [p.strip() for p in ports.split(",") if p.strip()]
    ports_yaml = "\n      - ".join(port_list)
    if ports_yaml:
        ports_yaml = "      - " + ports_yaml
    else:
        ports_yaml = "      - " + '"25565:25565"'

    server_port = port_list[0].split(":")[0] if port_list else "25565"    

    # Build plugins block outside the f-string to avoid backslashes inside f-string expressions
    plugins_block = ""
    if bedrock and server_type == 'PAPER':
        plugins_block = (
            "      PLUGINS: |\n"
            "        https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/spigot\n"
            "        https://download.geysermc.org/v2/projects/floodgate/versions/latest/builds/latest/downloads/spigot\n"
        )

    return f"""
version: "3.8"
services:
  mc:
    image: itzg/minecraft-server:latest
    container_name: mc_{name}
    tty: true
    stdin_open: true
    restart: unless-stopped
    ports:
{ports_yaml}
      - 25575:25575
    environment:
      EULA: "TRUE"
      SERVER_PORT: f"{server_port}"
      RCON_CMDS_STARTUP: |-
        gamerule playersSleepingPercentage 0
      VIEW_DISTANCE: "20"
      MOTD: f'"{os.getenv("CLOUDFLARE_ZONE_ID")}"'
      ICON: f'"{os.getenv("CLOUDFLARE_ZONE_ID")}"'
      ENABLE_WHITELIST: "true"
{plugins_block}
      TYPE: "{server_type}"
      VERSION: "{version}"
      MEMORY: "6144M"
      TZ: "Europe/Vienna"
      DIFFICULTY: "{difficulty}"
      ENABLE_RCON: "true"
      RCON_PASSWORD: "123"
      RCON_PORT: "25575"
    volumes:
      - "./data/{name}:/data"
  backups:
    image: itzg/mc-backup
    depends_on:
      - mc
    environment:
      BACKUP_INTERVAL: "12h"
      RCON_HOST: mc
      RCON_PORT: "25575"
      RCON_PASSWORD: "123"
      PAUSE_IF_PLAYERS_ONLINE: "true"
      INITIAL_DELAY: 0
    volumes:
      - ./data/{name}:/data:ro
      - ./data/mc-backups:/backups
""".strip()

def run_compose(name, command):
    server_dir = os.path.join(BASE_DIR, name)
    compose_file = os.path.join(server_dir, "docker-compose.yml")
    if not os.path.exists(compose_file):
        raise Exception(f"Server {name} existiert nicht.")
    subprocess.run(["docker", "compose", "-f", compose_file] + command, check=True, cwd=server_dir)

def server_exists(name):
    return os.path.exists(os.path.join(BASE_DIR, name, "docker-compose.yml"))

def save_server_info(name, difficulty, server_type, ports, version, bedrock=False):
    path = os.path.join(BASE_DIR, name, "server.info")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"difficulty: {difficulty}\nserver_type: {server_type}\nports: {ports}\nversion: {version}\nbedrock: {str(bool(bedrock))}\n")

def get_server_info(name):
    path = os.path.join(BASE_DIR, name, "server.info")
    if not os.path.exists(path): return None
    info = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
    return info

def is_running(name):
    c = f"mc_{name}"
    result = subprocess.run(["docker", "ps", "--filter", f"name=^{c}$", "--format", "{{.Names}}"], capture_output=True, text=True)
    return bool(result.stdout.strip())

@app.route('/stats/<name>')
def stats(name):
    """Return basic CPU and memory usage for the container as JSON.

    Uses `docker stats --no-stream --format` if available; falls back to parsing `docker stats --no-stream` output.
    """
    container_name = f"mc_{name}"

    # Check container exists
    if not is_running(name):
        return jsonify({"error": "container_not_running"}), 404

    try:
        # Try using a formatted output for easier parsing
        fmt = "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"
        proc = subprocess.run(["docker", "stats", "--no-stream", "--format", fmt, container_name], capture_output=True, text=True, check=True)
        out = proc.stdout.strip()
        # Expected: mc_name|0.13%|12.34MiB / 1GiB
        if out:
            parts = out.split("|", 2)
            if len(parts) == 3:
                name_out, cpu, mem = parts
                cpu = cpu.strip()
                # compute normalized cpu (percentage of total system capacity)
                cpu_pct = None
                cpu_normalized = None
                try:
                    m_cpu = re.match(r"([0-9\.]+)\s*%", cpu)
                    if m_cpu:
                        cpu_pct = float(m_cpu.group(1))
                        num = os.cpu_count() or 1
                        cpu_normalized = round(cpu_pct / num, 2)
                except Exception:
                    cpu_pct = None
                    cpu_normalized = None

                # parse mem into used and total if possible
                mem_used = None
                mem_total = None
                m = re.match(r"([0-9\.]+\w+)\s*/\s*([0-9\.]+\w+)", mem)
                if m:
                    mem_used = m.group(1)
                    mem_total = m.group(2)

                resp = {
                    "name": name_out,
                    "cpu_raw": cpu,
                    "mem": mem.strip(),
                    "mem_used": mem_used,
                    "mem_total": mem_total,
                    "num_cpus": os.cpu_count() or 1,
                }
                if cpu_pct is not None:
                    resp["cpu_pct"] = cpu_pct
                if cpu_normalized is not None:
                    resp["cpu_normalized_pct"] = cpu_normalized

                return jsonify(resp)

        # If we get here, unable to parse
        return jsonify({"error": "unable_to_parse_stats", "raw": out}), 500

    except subprocess.CalledProcessError as e:
        return jsonify({"error": "docker_error", "message": str(e), "stdout": e.stdout, "stderr": e.stderr}), 500

def list_servers():
    servers = []
    for name in os.listdir(BASE_DIR):
        info = get_server_info(name)
        if info:
            servers.append({
                "name": name,
                "difficulty": info.get("difficulty", "?"),
                "server_type": info.get("server_type", "?"),
                "ports": info.get("ports", "?"),
                "version": info.get("version", "LATEST"),
                "bedrock": info.get("bedrock", "False").lower() == 'true',
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

@app.route("/create", methods=["POST"])
def create_server():
    name = request.form["name"]
    difficulty = request.form["difficulty"]
    server_type = request.form["server_type"]
    version = request.form["version"]
    raw_ports = request.form["ports"]
    bedrock = request.form.get('bedrock') == 'true'

    # Prüfen, ob schon ein Server läuft
    running_servers = [s for s in list_servers() if s["running"]]
    if running_servers:
        return redirect(url_for("index", message=f"❌ Es läuft bereits der Server '{running_servers[0]['name']}'. Es kann immer nur ein Server gleichzeitig laufen."))

    if server_exists(name):
        return redirect(url_for("index", message=f"Server '{name}' existiert bereits."))

    mapped_ports = [f"{p}:{p}" for p in raw_ports.split(",") if p.strip()]
    # If bedrock support is requested for PAPER, add the UDP port mapping for Bedrock (19132)
    if bedrock and server_type == 'PAPER':
        # ensure we don't duplicate if user already included it
        if not any('19132' in p for p in mapped_ports):
            mapped_ports.append('19132:19132/udp')
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
        # fallback to standard Minecraft port
        srv_port = 25565

    server_dir = os.path.join(BASE_DIR, name)
    os.makedirs(server_dir, exist_ok=True)

    with open(os.path.join(server_dir, "docker-compose.yml"), "w", encoding="utf-8") as f:
        f.write(create_compose_yaml(name, difficulty, server_type, ports, version, bedrock=bedrock))

    save_server_info(name, difficulty, server_type, ports, version, bedrock=bedrock)

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
