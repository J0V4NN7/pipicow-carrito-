# -*- coding: utf-8 -*-
"""
Created on Wed Oct 15 23:31:18 2025

@author: Jovanny Lezama
"""

# -*- coding: utf-8 -*-
"""
Interfaz profesional Pico W + Simulador + HC-SR04 + Joystick (Firmata)
- Tema oscuro con acentos rojo/morado/negro
- Tacómetro (velocidad estimada por PWM), radar (blip) y gráfica de distancia en tiempo real
- CommandSender robusto (rate-limit, coalescing, backoff, Connection: close, shutdown)
- Compatible con Spyder (Tkinter)
Autor: integrado / modificado para petición del usuario
Requisitos: pip install pillow requests pyfirmata
"""
import math, time, os, threading, collections
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageFilter
import requests

# pyfirmata optional
try:
    from pyfirmata import Arduino, util
except Exception:
    Arduino = None
    util = None

# --------------------------- CONFIG ---------------------------
TIMEOUT = 1.5
RATE_LIMIT_MS = 160
MAX_FAILS = 4
ULTRA_POLL_MS = 180
JOY_POLL_MS = 50

# Arduino port (ajusta)
ARDUINO_PORT = "COM15"

# Visual/theme
BG = "#0b0b0d"          # fondo casi negro
CARD = "#111217"
ACCENT1 = "#ff2d55"     # rojo vibrante
ACCENT2 = "#9b59b6"     # morado
ACCENT_EDGE = "#0a0a0b"
TEXT = "#e6e6e6"
SUBTEXT = "#bfc4c8"

# Velocidad estimada: PWM 255 -> V_MAX_KMH
V_MAX_KMH = 12.0

# Radar / grafica buffer
DIST_HISTORY_LEN = 120

# ----------------------- Estado global ------------------------
estado = {"conectado": False}
_last_vel_ts = 0
_last_vel_val = -1

# -------------------- CommandSender (robusto) -----------------
class CommandSender:
    """Rate-limit + coalescing + retries + Connection: close + shutdown + backoff.
       after_fn(ms, callable) injection to schedule on main thread (root.after)."""
    def __init__(self, get_ip_callable, on_soft_error, on_hard_disconnect, after_fn=None):
        self.get_ip = get_ip_callable
        self.on_soft_error = on_soft_error
        self.on_hard_disconnect = on_hard_disconnect
        self._after = after_fn
        self.lock = threading.Lock()
        self.pending = None
        self.inflight = False
        self.last_ts = 0.0
        self.fail_count = 0
        self.shutdown = False

    def queue(self, cmd):
        with self.lock:
            self.pending = cmd
        self._pump()

    def _pump(self):
        with self.lock:
            if self.inflight or self.pending is None or self.shutdown:
                return
            now = time.time()*1000
            wait_ms = max(0, RATE_LIMIT_MS - int(now - self.last_ts))
        if wait_ms > 0:
            if self._after:
                self._after(wait_ms, self._pump)
            else:
                threading.Timer(wait_ms/1000.0, self._pump).start()
            return

        def _send():
            with self.lock:
                if self.pending is None or self.shutdown:
                    self.inflight = False
                    return
                cmd_local = self.pending
                self.pending = None
                self.inflight = True
            try:
                ip = self.get_ip().strip()
                if not ip:
                    raise RuntimeError("IP vacía")
                url = f"http://{ip}/{cmd_local}"
                r = requests.get(url, timeout=TIMEOUT,
                                 headers={"Connection":"close"},
                                 allow_redirects=False)
                if r.status_code == 200:
                    with self.lock:
                        self.fail_count = 0
                        self.last_ts = time.time()*1000
                else:
                    self._register_fail()
            except Exception:
                self._register_fail()
            finally:
                with self.lock:
                    self.inflight = False
            if self._after:
                self._after(0, self._pump)
            else:
                threading.Timer(0, self._pump).start()

        threading.Thread(target=_send, daemon=True).start()

    def _register_fail(self):
        with self.lock:
            self.fail_count += 1
            fc = self.fail_count
        if fc < MAX_FAILS:
            self.on_soft_error(f"Intento fallido ({fc}/{MAX_FAILS})")
            # backoff schedule (ms)
            back_ms = min(2000, fc * 200)
            if self._after:
                self._after(back_ms, self._pump)
            else:
                threading.Timer(back_ms/1000.0, self._pump).start()
        else:
            self.on_hard_disconnect("Múltiples fallos de envío.")

    def shutdown_sender(self):
        with self.lock:
            self.shutdown = True
            self.pending = None

# ----------------------- Utilidades UI -------------------------
def rounded_rect_image(w,h,r,fill1,fill2,outline=None):
    """Genera gradiente vertical redondeado para botones/panels"""
    im = Image.new("RGBA",(w,h),(0,0,0,0))
    draw = ImageDraw.Draw(im)
    top = tuple(int(fill1.lstrip("#")[i:i+2],16) for i in (0,2,4))
    bot = tuple(int(fill2.lstrip("#")[i:i+2],16) for i in (0,2,4))
    for y in range(h):
        t = y/(h-1) if h>1 else 0
        c = tuple(int(top[i]*(1-t)+bot[i]*t) for i in range(3))
        draw.line([(0,y),(w,y)], fill=(*c,255))
    mask = Image.new("L",(w,h),0)
    ImageDraw.Draw(mask).rounded_rectangle([0,0,w-1,h-1],radius=r,fill=255)
    im.putalpha(mask)
    if outline:
        ImageDraw.Draw(im).rounded_rectangle([0,0,w-1,h-1],radius=r,outline=outline,width=2)
    return ImageTk.PhotoImage(im)

def nice_button(master, text, w=140, h=42, command=None):
    """Crea un botón estilizado con PIL gradiente + texto"""
    img = rounded_rect_image(w,h,12,ACCENT2,ACCENT1,outline="#070707")
    b = tk.Button(master, image=img, text=text, compound="center", fg=TEXT, font=("Segoe UI",11,"bold"),
                  bd=0, activebackground=ACCENT1, cursor="hand2", relief="flat", command=command)
    b._img = img
    return b

# ----------------------- Ventana principal ---------------------
root = tk.Tk()
root.title("Pico W — Simulador profesional")
root.configure(bg=BG)
root.geometry("1240x820")

# ----------------------- Top: Conexión ------------------------
topf = tk.Frame(root, bg=BG)
topf.pack(fill="x", padx=12, pady=8)

tk.Label(topf, text="IP Pico W:", bg=BG, fg=TEXT, font=("Segoe UI",10)).pack(side="left")
ent_ip = tk.Entry(topf, width=16, font=("Segoe UI",11))
ent_ip.insert(0,"192.168.0.101")
ent_ip.pack(side="left", padx=(6,14))

btn_connect = nice_button(topf, "Verificar / Conectar", w=180, h=36)
btn_connect.pack(side="left")

lbl_status = tk.Label(topf, text="Desconectado", bg=BG, fg="#ff6b81", font=("Segoe UI",10,"bold"))
lbl_status.pack(side="left", padx=12)

# estado ultrasonico y bateria
status_frame = tk.Frame(topf, bg=BG)
status_frame.pack(side="right")
lbl_bat = tk.Label(status_frame, text="Batería: 12.0 V", bg=BG, fg=TEXT, font=("Segoe UI",10))
lbl_bat.pack(side="right", padx=(8,0))
lbl_ipinfo = tk.Label(status_frame, text="", bg=BG, fg=SUBTEXT, font=("Segoe UI",9))
lbl_ipinfo.pack(side="right", padx=(8,0))

# ----------------------- Left: Controles y botones ----------------
left = tk.Frame(root, bg=BG)
left.place(x=12, y=70, width=360, height=730)

card_img = rounded_rect_image(356,720,18,CARD,CARD,outline="#111116")
card_lbl = tk.Label(left, image=card_img, bg=BG)
card_lbl.image = card_img
card_lbl.place(x=0,y=0)

tk.Label(left, text="Controles", bg=CARD, fg=TEXT, font=("Segoe UI",14,"bold")).place(x=18,y=16)

# botones movement
btns_frame = tk.Frame(left, bg=CARD)
btns_frame.place(x=18,y=56,width=320,height=220)

btn_forward = nice_button(btns_frame, "Adelante", w=300, h=44, command=lambda: sender.queue("adelante"))
btn_backward= nice_button(btns_frame, "Atrás", w=300, h=44, command=lambda: sender.queue("atras"))
btn_leftc = nice_button(btns_frame, "Girar Izq", w=146, h=40, command=lambda: sender.queue("girar_izquierda"))
btn_rightc= nice_button(btns_frame, "Girar Der", w=146, h=40, command=lambda: sender.queue("girar_derecha"))
btn_stopc = nice_button(btns_frame, "DETENER", w=300, h=44, command=lambda: sender.queue("detener"))

btn_forward.place(x=10,y=6)
btn_backward.place(x=10,y=56)
btn_leftc.place(x=10,y=110)
btn_rightc.place(x=164,y=110)
btn_stopc.place(x=10,y=156)

# slider manual PWM
tk.Label(left, text="Control PWM (manual)", bg=CARD, fg=SUBTEXT, font=("Segoe UI",10)).place(x=18,y=296)
pwm_var = tk.IntVar(value=0)
def pwm_changed(v=None):
    v = pwm_var.get()
    _send_pwm(v)
sld = ttk.Scale(left, from_=0, to=255, orient="horizontal", command=lambda v: pwm_var.set(int(float(v))))
sld.place(x=22,y=326,width=292)
pwm_label = tk.Label(left, text="PWM: 0", bg=CARD, fg=TEXT, font=("Segoe UI",10,"bold"))
pwm_label.place(x=22,y=356)
pwm_var.trace_add("write", lambda *a: pwm_label.config(text=f"PWM: {pwm_var.get()}"))

# checkbox joystick enable
joy_enabled = tk.BooleanVar(value=True)
tk.Checkbutton(left, text="Joystick (Firmata) habilitado", variable=joy_enabled,
               bg=CARD, fg=TEXT, selectcolor=CARD, activebackground=CARD).place(x=20,y=388)

# indicador de velocidad (numérico)
speed_text = tk.StringVar(value="0.0 km/h")
tk.Label(left, text="Vel. estimada:", bg=CARD, fg=SUBTEXT, font=("Segoe UI",9)).place(x=20,y=416)
tk.Label(left, textvariable=speed_text, bg=CARD, fg=ACCENT1, font=("Segoe UI",18,"bold")).place(x=20,y=436)

# ----------------------- Right: Dashboard (tacometro, radar, grafica) ----------------
right = tk.Frame(root, bg=BG)
right.place(x=392, y=70, width=830, height=730)

# Dashboard card
card_r = rounded_rect_image(826,720,18,CARD,CARD,outline="#111116")
labr = tk.Label(right, image=card_r, bg=BG); labr.image = card_r
labr.place(x=0,y=0)

# Tacómetro canvas
tk.Label(right, text="Velocímetro", bg=CARD, fg=TEXT, font=("Segoe UI",12,"bold")).place(x=32,y=16)
taco_c = tk.Canvas(right, width=320, height=320, bg=CARD, highlightthickness=0)
taco_c.place(x=28,y=48)

# Radar canvas
tk.Label(right, text="Radar (ultrasonido)", bg=CARD, fg=TEXT, font=("Segoe UI",12,"bold")).place(x=384,y=16)
radar_c = tk.Canvas(right, width=360, height=360, bg=CARD, highlightthickness=0)
radar_c.place(x=380,y=48)

# Grafica linea (distancia)
tk.Label(right, text="Distancia (histórico)", bg=CARD, fg=TEXT, font=("Segoe UI",12,"bold")).place(x=32,y=392)
graph_c = tk.Canvas(right, width=708, height=200, bg="#0d0d0f", highlightthickness=0)
graph_c.place(x=56,y=420)

# ------------------ Variables de simulacion/lectura ----------------
dist_history = collections.deque(maxlen=DIST_HISTORY_LEN)
last_dist = None
objeto_presente = False
ultra_job = None
joy_job = None

# ------------------ CommandSender instance (después de root) --------------
sender = CommandSender(
    get_ip_callable=lambda: ent_ip.get(),
    on_soft_error=lambda m: lbl_status.config(text=m, fg=ACCENT2),
    on_hard_disconnect=lambda m: (lbl_status.config(text="Desconectado", fg="#ff6b81"), messagebox.showerror("Desconexión", m)),
    after_fn=lambda ms, fn: root.after(ms, fn)
)

# ------------------ HTTP alive check -----------------------
def http_alive(ip, timeout=TIMEOUT):
    try:
        r = requests.get(f"http://{ip}/ping", timeout=timeout, headers={"Connection":"close"}, allow_redirects=False)
        return r.status_code < 500
    except requests.exceptions.RequestException:
        return False

def verificar_conexion():
    ip = ent_ip.get().strip()
    if not ip:
        messagebox.showwarning("IP faltante", "Ingresa la dirección IP del Pico W.")
        return
    btn_connect.config(state="disabled")
    lbl_status.config(text="Verificando...", fg=SUBTEXT)
    def do():
        ok = http_alive(ip)
        if ok:
            estado["conectado"] = True
            lbl_status.config(text=f"Conectado {ip}", fg="#22c55e")
            lbl_ipinfo.config(text=f"→ {ip}")
            _start_ultra_poll()
        else:
            estado["conectado"] = False
            lbl_status.config(text="No responde", fg="#ff6b81")
            lbl_ipinfo.config(text="")
    threading.Thread(target=do, daemon=True).start()
    btn_connect.config(state="normal")

btn_connect.config(command=verificar_conexion)

# ------------------ Enviar PWM (con control de rate/backoff interno) -------------
_pwm_lock = threading.Lock()
def _send_pwm(pwm):
    global _last_vel_ts, _last_vel_val
    if not estado.get("conectado", False):
        return
    pwm = int(max(0, min(255, pwm)))
    now = time.time()*1000
    with _pwm_lock:
        if (abs(pwm - _last_vel_val) < 6) and (now - _last_vel_ts < 250):
            return
    try:
        ip = ent_ip.get().strip()
        if not ip: return
        r = requests.get(f"http://{ip}/velocidad?v={pwm}", timeout=TIMEOUT, headers={"Connection":"close"}, allow_redirects=False)
        if r.status_code == 200:
            _last_vel_val = pwm
            _last_vel_ts = now
            speed_kmh = (pwm/255.0) * V_MAX_KMH
            speed_text.set(f"{speed_kmh:.2f} km/h")
        else:
            pass
    except requests.exceptions.RequestException:
        pass

# small wrapper for UI triggered pwm
def send_pwm_ui():
    _send_pwm(pwm_var.get())

# ------------------ Ultrasonic polling & parse ------------------
def _parse_distance_response(r):
    try:
        j = r.json()
        if isinstance(j, dict) and "cm" in j:
            return float(j["cm"])
    except Exception:
        txt = r.text.strip()
        if txt and txt.lower() != "null":
            txt = txt.replace(",", ".")
            try:
                return float(txt)
            except Exception:
                return None
    return None

def _poll_ultrasonico():
    global ultra_job, last_dist, objeto_presente
    if not estado.get("conectado", False):
        # clear UI indicators
        draw_radar(None)
        append_dist(None)
        ultra_job = root.after(ULTRA_POLL_MS, _poll_ultrasonico)
        return
    ip = ent_ip.get().strip()
    try:
        r = requests.get(f"http://{ip}/distancia", timeout=TIMEOUT, headers={"Connection":"close"})
        d = _parse_distance_response(r)
        if d is not None:
            last_dist = d
            append_dist(d)
            # detection threshold (cm)
            threshold = 25
            detect = (d <= threshold)
            if detect:
                # visual + optional beep
                objeto_presente = True
            else:
                objeto_presente = False
        else:
            append_dist(None)
    except requests.exceptions.RequestException:
        append_dist(None)
    ultra_job = root.after(ULTRA_POLL_MS, _poll_ultrasonico)

def _start_ultra_poll():
    global ultra_job
    if ultra_job is None:
        ultra_job = root.after(40, _poll_ultrasonico)

def _stop_ultra_poll():
    global ultra_job
    if ultra_job is not None:
        root.after_cancel(ultra_job); ultra_job = None

# ------------------ Dist history & drawing ------------------
def append_dist(d):
    """Agregar distancia y redibujar gráficas / radar / tacómetro."""
    if d is None:
        dist_history.append(None)
    else:
        dist_history.append(float(d))
    draw_graph()
    draw_radar(dist_history[-1] if len(dist_history)>0 else None)

# Graph drawing (simple)
def draw_graph():
    graph_c.delete("all")
    w = int(graph_c["width"]); h = int(graph_c["height"])
    graph_c.create_rectangle(0,0,w,h,fill="#0d0d0f",outline="")
    # axes and labels
    graph_c.create_text(8,10,anchor="nw",text="Dist (cm)",fill=SUBTEXT,font=("Segoe UI",9))
    vals = [v for v in dist_history if v is not None]
    if not vals:
        graph_c.create_text(w//2,h//2, text="Sin datos", fill=SUBTEXT, font=("Segoe UI",12))
        return
    maxv = max(max(vals), 1.0)
    minv = min(vals)
    margin = 16
    plot_w = w-2*margin; plot_h = h-2*margin
    # draw polyline
    pts = []
    for i, v in enumerate(dist_history):
        x = margin + (i / max(1, (DIST_HISTORY_LEN-1))) * plot_w
        if v is None:
            y = margin + plot_h
        else:
            # invert: small distance -> low y
            norm = (v - 0) / (maxv - 0) if maxv>0 else 0
            y = margin + (1 - min(1, norm)) * plot_h
        pts.append((x,y))
    # fill under curve with color that changes when nearest is close
    nearest = min([v for v in dist_history if v is not None] or [999])
    fill_color = ACCENT2 if nearest > 40 else ACCENT1
    # polygon for fill
    poly = []
    for x,y in pts: poly.extend([x,y])
    if poly:
        poly = [margin+0, margin+plot_h] + poly + [margin+plot_w, margin+plot_h]
        graph_c.create_polygon(poly, fill=fill_color+"22", outline="")
    # polyline
    for i in range(1,len(pts)):
        graph_c.create_line(pts[i-1][0],pts[i-1][1],pts[i][0],pts[i][1], fill=fill_color, width=2, smooth=True)

# Radar draw
def draw_radar(d):
    radar_c.delete("all")
    w = int(radar_c["width"]); h = int(radar_c["height"])
    cx = w//2; cy = h//2+30
    radius = min(w,h)*0.42
    # background arcs (fades)
    for i in range(4):
        r = radius*(1 - i*0.22)
        alpha = int(60 - i*12)
        radar_c.create_oval(cx-r, cy-r, cx+r, cy+r, outline=ACCENT2, width=1)
    # sweep arc
    sweep_color = ACCENT1
    radar_c.create_arc(cx-radius, cy-radius, cx+radius, cy+radius, start=200, extent=140, style="arc", outline=sweep_color, width=3)
    # blip if distance present
    if d is None:
        radar_c.create_text(cx, cy, text="Sin datos", fill=SUBTEXT, font=("Segoe UI",12,"bold"))
        return
    # map distance (0..200cm) to radius
    d_clamped = max(0.0, min(200.0, float(d)))
    rpos = (1 - d_clamped/200.0) * radius * 0.95
    # choose angle (dynamic): use time to rotate blip for nicer visual
    angle_deg = (time.time()*60) % 140 + 200
    ang = math.radians(angle_deg)
    bx = cx + math.cos(ang) * rpos
    by = cy + math.sin(ang) * rpos
    # glow
    radar_c.create_oval(bx-14,by-14,bx+14,by+14, fill=ACCENT2+"55", outline="")
    radar_c.create_oval(bx-7,by-7,bx+7,by+7, fill=ACCENT1, outline="")
    # label
    radar_c.create_text(24,24, text=f"{d_clamped:.0f} cm", fill=TEXT, anchor="nw", font=("Segoe UI",14,"bold"))
    if d_clamped < 30:
        radar_c.create_text(24,48, text="OBJETO CERCANO!", fill="#ff3b4a", anchor="nw", font=("Segoe UI",11,"bold"))
    else:
        radar_c.create_text(24,48, text="Libre", fill="#9ae6b4", anchor="nw", font=("Segoe UI",11,"bold"))

# Tacómetro drawing
def draw_tacometro(pwm_val):
    taco_c.delete("all")
    w = int(taco_c["width"]); h = int(taco_c["height"])
    cx = w//2; cy = h//2+10
    r = 130
    # background circle
    taco_c.create_oval(cx-r,cy-r,cx+r,cy+r, fill="#070709", outline="#111114", width=3)
    # ticks
    steps = 10
    for i in range(steps+1):
        ang = math.radians(210 - (i*(240/steps)))
        x1 = cx + math.cos(ang)*(r-12); y1 = cy + math.sin(ang)*(r-12)
        x2 = cx + math.cos(ang)*(r-2);  y2 = cy + math.sin(ang)*(r-2)
        taco_c.create_line(x1,y1,x2,y2, fill="#222226", width=2)
        # label every 2 steps
        if i%2==0:
            lab = f"{(i/steps)*V_MAX_KMH:.0f}"
            xl = cx + math.cos(ang)*(r-30); yl = cy + math.sin(ang)*(r-30)
            taco_c.create_text(xl,yl,text=lab,fill=SUBTEXT,font=("Segoe UI",9,"bold"))
    # needle based on pwm->speed
    pwm = pwm_var.get()
    ang_deg = 210 - (pwm/255.0)*240
    ang = math.radians(ang_deg)
    nx = cx + math.cos(ang)*(r-70); ny = cy + math.sin(ang)*(r-70)
    # needle shadow/glow
    taco_c.create_line(cx,cy,nx,ny, fill=ACCENT2, width=12, capstyle="round")
    taco_c.create_line(cx,cy,nx,ny, fill=ACCENT1, width=6, capstyle="round")
    # center cap
    taco_c.create_oval(cx-10,cy-10,cx+10,cy+10,fill="#0d0d0f",outline=ACCENT1,width=2)
    # numeric
    speed_kmh = (pwm/255.0)*V_MAX_KMH
    speed_text.set(f"{speed_kmh:.2f} km/h")

# periodic UI update for animated components
def ui_tick():
    draw_tacometro(pwm_var.get())
    # update radar rotating glow even if no new measure
    if dist_history:
        draw_radar(dist_history[-1])
    root.after(80, ui_tick)

root.after(80, ui_tick)

# ------------------ Firmata joystick (opcional) ------------------
js_board = None
x1_pin = y1_pin = x2_pin = y2_pin = None
last_dir_cmd = None

def _norm01_to_sym(v, dead=0.08):
    if v is None: return 0.0
    v = max(0.0, min(1.0, float(v)))
    c = (v - 0.5) * 2.0
    if abs(c) < dead: return 0.0
    if c > 0: c = (c - dead) / (1-dead)
    else:     c = (c + dead) / (1-dead)
    return max(-1.0, min(1.0, c))

def firmata_connect(port=None):
    global js_board, x1_pin, y1_pin, x2_pin, y2_pin
    if not joy_enabled.get():
        messagebox.showinfo("Joystick", "Joystick está deshabilitado en la UI.")
        return
    if Arduino is None:
        messagebox.showerror("pyfirmata faltante", "Instala pyfirmata: pip install pyfirmata")
        return
    try:
        p = (port or ARDUINO_PORT)
        js_board = Arduino(p)
        it = util.Iterator(js_board); it.start()
        x1_pin = js_board.get_pin('a:0:i'); y1_pin = js_board.get_pin('a:1:i')
        x2_pin = js_board.get_pin('a:3:i'); y2_pin = js_board.get_pin('a:2:i')
        for pin in (x1_pin,y1_pin,x2_pin,y2_pin):
            if pin: pin.enable_reporting()
        lbl_status.config(text=f"Joystick: conectado {p}", fg="#22c55e")
        start_joy_poll()
    except Exception as e:
        messagebox.showerror("Firmata error", str(e))
        js_board = None

def firmata_disconnect():
    global js_board, x1_pin, y1_pin, x2_pin, y2_pin
    stop_joy_poll()
    try:
        if js_board: js_board.exit()
    except: pass
    js_board=None; x1_pin=y1_pin=x2_pin=y2_pin=None
    lbl_status.config(text="Joystick desconectado", fg=SUBTEXT)

def _joy_arcade_step():
    if x1_pin is None or y1_pin is None: return
    x = x1_pin.read(); y = y1_pin.read()
    nx = _norm01_to_sym(x); ny = _norm01_to_sym(y)
    mag = max(abs(nx),abs(ny))
    if mag < 0.05:
        return
    # decide direction
    if abs(ny) >= abs(nx):
        cmd = "adelante" if ny>0 else "atras"
    else:
        cmd = "girar_derecha" if nx>0 else "girar_izquierda"
    if cmd:
        sender.queue(cmd)
    # send estimated pwm proportional to magnitude
    pwm = int(120 + (255-120)*mag)
    pwm_var.set(pwm); _send_pwm(pwm)

def _joy_poll():
    try:
        if js_board is not None:
            _joy_arcade_step()
    except Exception:
        pass
    global joy_job
    joy_job = root.after(JOY_POLL_MS, _joy_poll)

def start_joy_poll():
    global joy_job
    if joy_job is None:
        joy_job = root.after(200, _joy_poll)

def stop_joy_poll():
    global joy_job
    if joy_job is not None:
        root.after_cancel(joy_job); joy_job = None

# joystick buttons in UI
btn_js_conn = nice_button(left, "Conectar Joystick", w=300, h=36, command=lambda: firmata_connect(ARDUINO_PORT))
btn_js_conn.place(x=22,y=464)

# ------------------ Cleanup on close ------------------
def _on_close():
    try: firmata_disconnect()
    except: pass
    try: sender.shutdown_sender()
    except: pass
    try: _stop_ultra_poll()
    except: pass
    try: stop_joy_poll()
    except: pass
    root.destroy()

root.protocol("WM_DELETE_WINDOW", _on_close)

# ------------------ Bindings ------------------
# Space => stop
def on_key(e):
    k = e.keysym
    if k == "space":
        sender.queue("detener")
root.bind("<KeyPress>", on_key)

# Send pwm button
btn_pwm_send = nice_button(left, "Enviar PWM", w=300, h=36, command=send_pwm_ui)
btn_pwm_send.place(x=22,y=518)

# Start/Stop distance polling controls
btn_start_ultra = nice_button(left, "Iniciar Ultrasonido", w=300, h=36, command=_start_ultra_poll)
btn_stop_ultra  = nice_button(left, "Detener Ultrasonido", w=300, h=36, command=_stop_ultra_poll)
btn_start_ultra.place(x=22,y=566)
btn_stop_ultra.place(x=22,y=616)

# ------------------ Inicialización ------------------
def init_ui_state():
    draw_graph()
    draw_tacometro(pwm_var.get())

init_ui_state()

# ------------------ Start mainloop ------------------
if __name__ == "__main__":
    root.mainloop()
