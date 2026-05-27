"""
=============================================================================
  BENCH PRESS ENCODER GAME
  Connects to an AM4096 encoder via Arduino (SSI over serial) and renders
  a videogame-style interface where the user guides a ball along a triangle-
  wave path by pressing the bench-press machine at the correct velocity.
=============================================================================

  ──────────────────────────────────────────────────────────────────────────
  CONFIGURATION  ←  EDIT THESE VALUES BEFORE RUNNING
  ──────────────────────────────────────────────────────────────────────────
"""

# --- Serial port where your Arduino is connected ---
SERIAL_PORT = "COM7"          # Windows example: "COM3"  |  Mac/Linux: "/dev/ttyUSB0"
BAUD_RATE   = 115200

# --- Encoder angle range (degrees) ---
# Set to the actual min/max angles your encoder reads during the full
# range of motion of the bench-press machine.
MIN_ANGLE   = 301.0          # angle (°) at the bottom  (bar fully lowered)
MAX_ANGLE   = 280.0           # angle (°) at the top     (bar fully pressed up)

# --- Invert Direction ---
# False: Ball goes UP when angle INCREASES.
# True:  Ball goes UP when angle DECREASES.
INVERT_DIRECTION = True

# --- Path Speeds and Angles ---
# GAME_SPEED: How fast the world automatically scrolls (pixels per second).
# Higher values mean you have to execute the reps faster!
GAME_SPEED     = 200.0       

# UPHILL_ANGLE / DOWNHILL_ANGLE: The steepness of the path in degrees (10° to 80°).
# 80° = very steep (requires fast pressing)
# 10° = low slope  (requires slow, controlled pressing)
UPHILL_ANGLE   = 60.0        # Pressing phase slope
DOWNHILL_ANGLE = 35.0        # Lowering phase slope

# --- Coins ---
COINS_PER_REP = 5            # how many coins are placed along each rep cycle
COIN_RADIUS   = 25           # how close the ball must be to collect a coin

# --- Stroke Calculation ---
STROKE_START_ANGLE = 301.55  # retracted
STROKE_END_ANGLE   = 263.23  # extended
L_RETRACTED_MM     = 809.607
L_EXTENDED_MM      = 476.640

# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import math
import time
import csv
import threading
import collections
import re
import pygame
import serial
import serial.tools.list_ports
from datetime import datetime

# ── constants ──────────────────────────────────────────────────────────────
SCREEN_W, SCREEN_H = 1280, 720
FPS          = 60
BALL_RADIUS  = 18
PATH_Y_TOP   = 120            # y-pixel for MAX_ANGLE  (top of screen area)
PATH_Y_BOT   = SCREEN_H - 120 # y-pixel for MIN_ANGLE  (bottom)

# ── colours ───────────────────────────────────────────────────────────────
BG          = (10,  12,  20)
GRID        = (25,  30,  50)
PATH_COL    = (0,  200, 255)
BALL_COL    = (255, 80,  80)
BALL_GLOW   = (255, 160, 120)
COIN_COL    = (255, 215,  0)
COIN_SHINE  = (255, 255, 180)
TEXT_COL    = (220, 230, 255)
HUD_COL     = (0,  200, 255)
GREEN       = (80,  230, 100)
ORANGE      = (255, 160,  40)
WHITE       = (255, 255, 255)
SHADOW      = (0,    0,   0, 120)

# ─────────────────────────────────────────────────────────────────────────────
# SERIAL READER  (runs in a background thread)
# ─────────────────────────────────────────────────────────────────────────────
class EncoderReader(threading.Thread):
    """Reads angle values from Arduino serial output in a background thread."""
    ANGLE_RE = re.compile(r"Angle:\s*([\d.]+)\s*°?", re.IGNORECASE)

    def __init__(self, port, baud):
        super().__init__(daemon=True)
        self.port   = port
        self.baud   = baud
        self._angle = MIN_ANGLE          # start at bottom
        self._lock  = threading.Lock()
        self._connected = False
        self._error     = ""

    @property
    def angle(self):
        with self._lock:
            return self._angle

    @property
    def connected(self):
        return self._connected

    @property
    def error(self):
        return self._error

    def run(self):
        try:
            ser = serial.Serial(self.port, self.baud, timeout=1)
            self._connected = True
            while True:
                try:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    m = self.ANGLE_RE.search(line)
                    if m:
                        val = float(m.group(1))
                        with self._lock:
                            self._angle = val
                except Exception:
                    pass
        except serial.SerialException as e:
            self._error = str(e)

# ─────────────────────────────────────────────────────────────────────────────
# ANGLE → SCREEN Y  (AND Y → ANGLE FOR LOGGING)
# ─────────────────────────────────────────────────────────────────────────────
def angle_to_y(angle):
    """Map encoder angle to a vertical pixel position on screen."""
    low_val = min(MIN_ANGLE, MAX_ANGLE)
    high_val = max(MIN_ANGLE, MAX_ANGLE)
    
    span = high_val - low_val
    if span < 0.001:
        span = 0.001
        
    t = (angle - low_val) / span
    
    if INVERT_DIRECTION:
        t = 1.0 - t
        
    t = max(0.0, min(1.0, t))
    return int(PATH_Y_BOT - t * (PATH_Y_BOT - PATH_Y_TOP))

def y_to_angle(y):
    """Map vertical screen pixel position back to encoder angle for the CSV."""
    low_val = min(MIN_ANGLE, MAX_ANGLE)
    high_val = max(MIN_ANGLE, MAX_ANGLE)
    span = high_val - low_val
    if span < 0.001:
        span = 0.001
        
    dy = PATH_Y_BOT - PATH_Y_TOP
    if dy == 0:
        return low_val
        
    t = (PATH_Y_BOT - y) / dy
    
    if INVERT_DIRECTION:
        t = 1.0 - t
        
    return t * span + low_val

# ─────────────────────────────────────────────────────────────────────────────
# STROKE CALCULATION
# ─────────────────────────────────────────────────────────────────────────────
def calculate_stroke(angle):
    """Convert the current angle to a stroke length in mm."""
    angle_span = STROKE_START_ANGLE - STROKE_END_ANGLE
    if angle_span == 0:
        return 0.0
        
    full_stroke_mm = abs(L_RETRACTED_MM - L_EXTENDED_MM)
    stroke_fraction = (STROKE_START_ANGLE - angle) / angle_span
    
    # Clip between 0.0 and 1.0 (Replaces numpy.clip)
    stroke_fraction = max(0.0, min(1.0, stroke_fraction))
    
    return stroke_fraction * full_stroke_mm

# ─────────────────────────────────────────────────────────────────────────────
# CSV SAVING FUNCTION
# ─────────────────────────────────────────────────────────────────────────────
def save_recording(data_log, coins_collected, total_coins):
    """Save the recorded angles and coins to a CSV file."""
    os.makedirs("recordings", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"recordings/bench_press_log_{timestamp}.csv"
    
    with open(filename, mode='w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["# BENCH PRESS GAME RESULTS"])
        writer.writerow([f"# Coins Collected: {coins_collected}"])
        writer.writerow([f"# Total Coins: {total_coins}"])
        pct = int(100 * coins_collected / max(total_coins, 1))
        writer.writerow([f"# Percentage: {pct}%"])
        writer.writerow([])
        # Added Stroke (mm) column here
        writer.writerow(["Time (s)", "Target Angle (deg)", "Actual Angle (deg)", "Stroke (mm)"])
        writer.writerows(data_log)
    print(f"Game data safely stored to {filename}")

# ─────────────────────────────────────────────────────────────────────────────
# PATH GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
class PathSegment:
    """One up-slope or down-slope segment of the triangle wave."""
    def __init__(self, x_start, y_start, x_end, y_end):
        self.x0, self.y0 = x_start, y_start
        self.x1, self.y1 = x_end,   y_end

    def y_at(self, x):
        if self.x1 == self.x0:
            return self.y0
        t = (x - self.x0) / (self.x1 - self.x0)
        return self.y0 + t * (self.y1 - self.y0)

def build_path(num_reps, coins_per_rep):
    """
    Build the full triangle-wave path and coin positions for num_reps.
    Calculates distance horizontally based on strictly enforced slope angles.
    """
    segments = []
    coins    = []
    x = 0
    y_bot = PATH_Y_BOT
    y_top = PATH_Y_TOP
    dy = y_bot - y_top

    # Ensure angle bounds (10 to 80 degrees) to prevent math errors (tan(0) / tan(90))
    up_ang = max(10.0, min(80.0, UPHILL_ANGLE))
    dn_ang = max(10.0, min(80.0, DOWNHILL_ANGLE))

    # Trigonometry: delta_X = delta_Y / tan(angle)
    up_width   = dy / math.tan(math.radians(up_ang))
    down_width = dy / math.tan(math.radians(dn_ang))

    for rep in range(num_reps):
        # ── uphill (press) ──────────────────────────────────────────────
        seg_up = PathSegment(x, y_bot, x + up_width, y_top)
        segments.append(seg_up)
        # place coins evenly along uphill
        for k in range(1, coins_per_rep + 1):
            t  = k / (coins_per_rep + 1)
            cx = seg_up.x0 + t * (seg_up.x1 - seg_up.x0)
            cy = seg_up.y0 + t * (seg_up.y1 - seg_up.y0)
            coins.append([cx, cy, False])   # [x, y, collected]
        x += up_width

        # ── downhill (lower) ────────────────────────────────────────────
        seg_dn = PathSegment(x, y_top, x + down_width, y_bot)
        segments.append(seg_dn)
        # place coins evenly along downhill
        for k in range(1, coins_per_rep + 1):
            t  = k / (coins_per_rep + 1)
            cx = seg_dn.x0 + t * (seg_dn.x1 - seg_dn.x0)
            cy = seg_dn.y0 + t * (seg_dn.y1 - seg_dn.y0)
            coins.append([cx, cy, False])   # [x, y, collected]
        x += down_width

    return segments, coins

# ─────────────────────────────────────────────────────────────────────────────
# DRAWING HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def draw_grid(surface, offset_x):
    spacing = 80
    start_x = -(offset_x % spacing)
    for gx in range(int(start_x), SCREEN_W + spacing, spacing):
        pygame.draw.line(surface, GRID, (gx, 0), (gx, SCREEN_H))
    for gy in range(0, SCREEN_H, spacing):
        pygame.draw.line(surface, GRID, (0, gy), (SCREEN_W, gy))

def draw_path(surface, segments, offset_x, highlight_seg=None):
    for i, seg in enumerate(segments):
        sx0 = int(seg.x0 - offset_x)
        sy0 = seg.y0
        sx1 = int(seg.x1 - offset_x)
        sy1 = seg.y1
        if sx1 < -50 or sx0 > SCREEN_W + 50:
            continue
            
        # Simplified shadow logic fixes huge lag spikes
        pygame.draw.line(surface, (30, 60, 90), (sx0, sy0 + 6), (sx1, sy1 + 6), 4)
        
        col = (0, 255, 180) if i == highlight_seg else PATH_COL
        pygame.draw.line(surface, col, (sx0, sy0), (sx1, sy1), 4)

def draw_coin(surface, sx, sy, collected, tick):
    if collected:
        return
    # pulsing scale
    scale = 1.0 + 0.08 * math.sin(tick * 0.12)
    r = int(COIN_RADIUS * scale)
    pygame.draw.circle(surface, COIN_COL,   (sx, sy), r)
    pygame.draw.circle(surface, COIN_SHINE, (sx, sy), max(r - 5, 4))
    pygame.draw.circle(surface, COIN_COL,   (sx, sy), r, 2)

# Pre-render ball glow to completely eliminate lag!
_cached_glow = None

def draw_ball(surface, bx, by, tick):
    global _cached_glow
    max_glow_r = BALL_RADIUS + 16
    
    if _cached_glow is None:
        size = max_glow_r * 2
        _cached_glow = pygame.Surface((size, size), pygame.SRCALPHA)
        for gr in range(max_glow_r, BALL_RADIUS - 1, -2):
            alpha = max(0, 180 - (gr - BALL_RADIUS) * 18)
            pygame.draw.circle(_cached_glow, (*BALL_GLOW, alpha), (max_glow_r, max_glow_r), gr)
            
    # Apply pre-rendered glow surface
    surface.blit(_cached_glow, (bx - max_glow_r, by - max_glow_r))
    pygame.draw.circle(surface, BALL_COL,  (bx, by), BALL_RADIUS)
    pygame.draw.circle(surface, WHITE,     (bx - 5, by - 5), 5)


def draw_text_shadow(surface, font, text, x, y, colour, shadow_col=(0,0,0)):
    s = font.render(text, True, shadow_col)
    surface.blit(s, (x + 2, y + 2))
    t = font.render(text, True, colour)
    surface.blit(t, (x, y))

def draw_hud(surface, fonts, reps_done, total_reps, coins_collected, total_coins, angle):
    f_big, f_med, f_sm = fonts
    # top bar
    pygame.draw.rect(surface, (15, 18, 35), (0, 0, SCREEN_W, 60))
    pygame.draw.line(surface, HUD_COL, (0, 60), (SCREEN_W, 60), 2)

    draw_text_shadow(surface, f_med, f"REPS: {reps_done} / {total_reps}", 20, 15, HUD_COL)
    coin_txt = f"⬤ COINS: {coins_collected} / {total_coins}"
    draw_text_shadow(surface, f_med, coin_txt, 320, 15, COIN_COL)
    angle_txt = f"ANGLE: {angle:.1f}°"
    draw_text_shadow(surface, f_med, angle_txt, SCREEN_W - 240, 15, GREEN)

    # progress bar
    bar_w = 400
    bar_x = SCREEN_W // 2 - bar_w // 2
    pygame.draw.rect(surface, GRID,    (bar_x, 20, bar_w, 22), border_radius=11)
    fill = int(bar_w * (reps_done / max(total_reps, 1)))
    if fill > 0:
        pygame.draw.rect(surface, HUD_COL, (bar_x, 20, fill, 22), border_radius=11)
    pygame.draw.rect(surface, HUD_COL, (bar_x, 20, bar_w, 22), 2, border_radius=11)

# ─────────────────────────────────────────────────────────────────────────────
# SETUP SCREEN
# ─────────────────────────────────────────────────────────────────────────────
def setup_screen(screen, fonts, encoder):
    f_big, f_med, f_sm = fonts
    reps  = 5
    clock = pygame.time.Clock()

    btn_rect  = pygame.Rect(SCREEN_W // 2 - 120, SCREEN_H - 180, 240, 60)
    up_rect   = pygame.Rect(SCREEN_W // 2 + 70,  SCREEN_H // 2 - 120, 50, 50)
    down_rect = pygame.Rect(SCREEN_W // 2 - 120, SCREEN_H // 2 - 120, 50, 50)

    tick = 0
    while True:
        tick += 1
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit(); sys.exit()
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_UP:
                    reps = min(reps + 1, 30)
                if event.key == pygame.K_DOWN:
                    reps = max(reps - 1, 1)
                if event.key == pygame.K_RETURN:
                    return reps
            if event.type == pygame.MOUSEBUTTONDOWN:
                if btn_rect.collidepoint(event.pos):
                    return reps
                if up_rect.collidepoint(event.pos):
                    reps = min(reps + 1, 30)
                if down_rect.collidepoint(event.pos):
                    reps = max(reps - 1, 1)

        screen.fill(BG)
        # animated grid
        draw_grid(screen, tick)

        # title
        title  = f_big.render("BENCH PRESS", True, HUD_COL)
        title2 = f_big.render("TRAINER", True, BALL_COL)
        screen.blit(title,  (SCREEN_W // 2 - title.get_width() // 2,  80))
        screen.blit(title2, (SCREEN_W // 2 - title2.get_width() // 2, 160))

        # connection status
        if encoder.connected:
            status_col = GREEN
            status_txt = f"✓  Encoder connected  ({SERIAL_PORT})"
        elif encoder.error:
            status_col = BALL_COL
            status_txt = f"✗  {encoder.error[:60]}"
        else:
            status_col = ORANGE
            status_txt = "Connecting to encoder …"
        st = f_sm.render(status_txt, True, status_col)
        screen.blit(st, (SCREEN_W // 2 - st.get_width() // 2, 250))

        # live angle readout
        angle_disp = f_med.render(f"Live angle: {encoder.angle:.1f}°", True, TEXT_COL)
        screen.blit(angle_disp, (SCREEN_W // 2 - angle_disp.get_width() // 2, 295))

        # reps selector
        label = f_med.render("NUMBER OF REPS", True, TEXT_COL)
        screen.blit(label, (SCREEN_W // 2 - label.get_width() // 2, SCREEN_H // 2 - 170))

        pygame.draw.rect(screen, GRID,   up_rect,   border_radius=8)
        pygame.draw.rect(screen, HUD_COL, up_rect, 2, border_radius=8)
        arr_u = f_med.render("▲", True, HUD_COL)
        screen.blit(arr_u, (up_rect.centerx - arr_u.get_width() // 2,
                            up_rect.centery - arr_u.get_height() // 2))

        pygame.draw.rect(screen, GRID,   down_rect, border_radius=8)
        pygame.draw.rect(screen, HUD_COL, down_rect, 2, border_radius=8)
        arr_d = f_med.render("▼", True, HUD_COL)
        screen.blit(arr_d, (down_rect.centerx - arr_d.get_width() // 2,
                            down_rect.centery - arr_d.get_height() // 2))

        reps_surf = f_big.render(str(reps), True, WHITE)
        screen.blit(reps_surf, (SCREEN_W // 2 - reps_surf.get_width() // 2,
                                SCREEN_H // 2 - 130))

        hint = f_sm.render("or press  ↑ / ↓  keys to change  |  ENTER to start", True, GRID)
        screen.blit(hint, (SCREEN_W // 2 - hint.get_width() // 2, SCREEN_H // 2 - 40))

        # START button
        hover = btn_rect.collidepoint(pygame.mouse.get_pos())
        btn_col = HUD_COL if hover else (0, 120, 180)
        pygame.draw.rect(screen, btn_col, btn_rect, border_radius=14)
        btn_txt = f_med.render("START", True, BG if hover else WHITE)
        screen.blit(btn_txt, (btn_rect.centerx - btn_txt.get_width() // 2,
                              btn_rect.centery - btn_txt.get_height() // 2))

        pygame.display.flip()
        clock.tick(FPS)

# ─────────────────────────────────────────────────────────────────────────────
# RESULTS SCREEN
# ─────────────────────────────────────────────────────────────────────────────
def results_screen(screen, fonts, coins_collected, total_coins):
    f_big, f_med, f_sm = fonts
    pct   = int(100 * coins_collected / max(total_coins, 1))
    clock = pygame.time.Clock()
    tick  = 0

    btn_rect = pygame.Rect(SCREEN_W // 2 - 140, SCREEN_H - 160, 280, 64)

    while True:
        tick += 1
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit(); sys.exit()
            if event.type == pygame.KEYDOWN:
                return
            if event.type == pygame.MOUSEBUTTONDOWN:
                if btn_rect.collidepoint(event.pos):
                    return

        screen.fill(BG)
        draw_grid(screen, tick)

        # confetti-ish particles
        for i in range(30):
            px = (i * 137 + tick * 3) % SCREEN_W
            py = (i * 97  + tick * 2) % SCREEN_H
            col = [COIN_COL, HUD_COL, BALL_COL, GREEN][i % 4]
            pygame.draw.circle(screen, col, (px, py), 4)

        # main message
        cong = f_big.render("CONGRATULATIONS!", True, COIN_COL)
        screen.blit(cong, (SCREEN_W // 2 - cong.get_width() // 2, 140))

        fin  = f_med.render("You have finished the set!", True, TEXT_COL)
        screen.blit(fin, (SCREEN_W // 2 - fin.get_width() // 2, 240))

        score = f_big.render(f"{pct}%  coins collected", True, WHITE)
        screen.blit(score, (SCREEN_W // 2 - score.get_width() // 2, 310))

        detail = f_sm.render(f"({coins_collected} out of {total_coins})", True, GRID)
        screen.blit(detail, (SCREEN_W // 2 - detail.get_width() // 2, 390))

        # grade
        if pct == 100:   grade, gc = "PERFECT!", GREEN
        elif pct >= 80:  grade, gc = "EXCELLENT", HUD_COL
        elif pct >= 60:  grade, gc = "GOOD JOB",  COIN_COL
        else:            grade, gc = "KEEP TRAINING", BALL_COL
        g_surf = f_big.render(grade, True, gc)
        screen.blit(g_surf, (SCREEN_W // 2 - g_surf.get_width() // 2, 430))

        # play again button
        hover = btn_rect.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(screen, HUD_COL if hover else (0, 100, 160),
                         btn_rect, border_radius=14)
        pa = f_med.render("PLAY AGAIN", True, BG if hover else WHITE)
        screen.blit(pa, (btn_rect.centerx - pa.get_width() // 2,
                         btn_rect.centery - pa.get_height() // 2))

        pygame.display.flip()
        clock.tick(FPS)

# ─────────────────────────────────────────────────────────────────────────────
# GAME LOOP
# ─────────────────────────────────────────────────────────────────────────────
def game_loop(screen, fonts, encoder, total_reps):
    f_big, f_med, f_sm = fonts
    clock = pygame.time.Clock()

    segments, coins = build_path(total_reps, COINS_PER_REP)
    total_coins     = len([c for c in coins if not c[2]])

    # The ball always appears at a fixed screen X; the world scrolls automatically
    BALL_SCREEN_X = SCREEN_W // 3
    world_x       = 0.0        # absolute world-x progress

    reps_done        = 0
    coins_collected  = 0
    tick             = 0
    current_seg_idx  = 0
    
    # Data logging variables
    recording_data   = []
    time_elapsed     = 0.0

    # collect spark particles
    sparks = []   # [(x, y, vx, vy, life, colour)]

    while True:
        tick += 1
        dt = clock.tick(FPS) / 1000.0
        time_elapsed += dt

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit(); sys.exit()
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                save_recording(recording_data, coins_collected, total_coins)
                return coins_collected, total_coins

        # ── Advance world_x automatically by Game Speed ────────────────────
        world_x += GAME_SPEED * dt

        # Update visual offset so ball stays fixed at BALL_SCREEN_X
        offset_x = world_x - BALL_SCREEN_X

        # ── find which segment the ball is currently in ────────────────────
        for i, seg in enumerate(segments):
            if seg.x0 <= world_x <= seg.x1:
                current_seg_idx = i
                break
                
        # Calculate reps based on segments passed (each rep has 2 segments: up, down)
        reps_done = current_seg_idx // 2
        phase     = current_seg_idx % 2   # 0=pressing up, 1=lowering

        # Check for game end condition
        if world_x >= segments[-1].x1:
            reps_done = total_reps
            save_recording(recording_data, coins_collected, total_coins)
            return coins_collected, total_coins

        # ── ball position (controlled entirely by user angle) ───────────────
        angle        = encoder.angle
        ball_world_y = angle_to_y(angle)
        ball_sx      = BALL_SCREEN_X
        ball_sy      = ball_world_y

        # ── data logging ────────────────────────────────────────────────
        seg = segments[min(current_seg_idx, len(segments) - 1)]
        target_y = int(seg.y_at(world_x))
        target_angle = y_to_angle(target_y)
        
        # Stroke calculation using the new function
        actual_stroke = calculate_stroke(angle)
        
        recording_data.append([
            round(time_elapsed, 3), 
            round(target_angle, 2), 
            round(angle, 2),
            round(actual_stroke, 2)
        ])

        # ── coin collection ─────────────────────────────────────────────
        for coin in coins:
            if coin[2]:
                continue
            coin_sx = int(coin[0] - offset_x)
            coin_sy = int(coin[1])
            dist    = math.hypot(coin_sx - ball_sx, coin_sy - ball_sy)
            
            if dist < COIN_RADIUS + BALL_RADIUS:
                coin[2] = True
                coins_collected += 1
                # spawn sparks
                for _ in range(12):
                    a = math.radians((_ * 30) + tick)
                    sparks.append([coin_sx, coin_sy,
                                   math.cos(a) * 4, math.sin(a) * 4,
                                   30, COIN_COL])

        # ── update sparks ────────────────────────────────────────────────
        for sp in sparks[:]:
            sp[0] += sp[2]; sp[1] += sp[3]; sp[4] -= 1
            if sp[4] <= 0:
                sparks.remove(sp)

        # ── DRAW ─────────────────────────────────────────────────────────
        screen.fill(BG)
        draw_grid(screen, int(offset_x))
        draw_path(screen, segments, offset_x, highlight_seg=current_seg_idx)

        # coins
        for coin in coins:
            sx = int(coin[0] - offset_x)
            draw_coin(screen, sx, int(coin[1]), coin[2], tick)

        # sparks
        for sp in sparks:
            alpha = int(255 * sp[4] / 30)
            pygame.draw.circle(screen, sp[5], (int(sp[0]), int(sp[1])), 4)

        # ball
        draw_ball(screen, ball_sx, ball_sy, tick)

        # path target guide line (vertical)
        diff = abs(ball_sy - target_y)
        tol  = 40
        guide_col = GREEN if diff < tol else (ORANGE if diff < tol * 2 else BALL_COL)
        pygame.draw.line(screen, (*guide_col, 80),
                         (ball_sx, ball_sy), (ball_sx, target_y), 2)

        # HUD
        draw_hud(screen, fonts, reps_done, total_reps,
                 coins_collected, total_coins, angle)

        # phase indicator
        phase_txt = "↑ PRESS UP" if phase == 0 else "↓ LOWER DOWN"
        ph = f_sm.render(phase_txt, True, GREEN if phase == 0 else ORANGE)
        screen.blit(ph, (SCREEN_W - ph.get_width() - 20, 70))

        pygame.display.flip()

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    pygame.init()
    pygame.display.set_caption("Bench Press Trainer")
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))

    # fonts — using pygame built-in bold as fallback; SysFont picks available
    try:
        f_big = pygame.font.SysFont("Consolas",      56, bold=True)
        f_med = pygame.font.SysFont("Consolas",      30, bold=True)
        f_sm  = pygame.font.SysFont("Consolas",      20)
    except Exception:
        f_big = pygame.font.Font(None, 64)
        f_med = pygame.font.Font(None, 36)
        f_sm  = pygame.font.Font(None, 24)
    fonts = (f_big, f_med, f_sm)

    # start encoder reader
    encoder = EncoderReader(SERIAL_PORT, BAUD_RATE)
    encoder.start()

    while True:
        total_reps                   = setup_screen(screen, fonts, encoder)
        coins_collected, total_coins = game_loop(screen, fonts, encoder, total_reps)
        results_screen(screen, fonts, coins_collected, total_coins)

if __name__ == "__main__":
    main()
