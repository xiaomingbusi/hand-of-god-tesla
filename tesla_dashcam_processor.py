import csv
import os
import re
import struct
import subprocess
import math
import json
import shutil
import hashlib
import time
import colorsys
import glob
import tempfile
import urllib.request
import urllib.parse
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageFilter
from datetime import datetime, timedelta

GRAVITY = 9.81
GFORCE_SCALE = 25
GFORCE_HISTORY_MAX = 3
FPS = 36
# 转向灯闪烁节奏（对齐 Sentry-Six）：特斯拉实车 400ms 亮 / 300ms 灭 = 700ms 周期
# 36fps 下每周期 25 帧，前 14 帧亮（14/36 ≈ 389ms ≈ 400ms）
BLINKER_CYCLE_FRAMES = 25
BLINKER_ON_FRAMES = 14
MPS_TO_KMH = 3.6
MPS_TO_MPH = 2.23694

COLORS = {
    'white': (255, 255, 255),
    'dim_white': (160, 160, 160),
    'green': (0, 200, 83),  # 转向灯：高饱和纯绿（对齐特斯拉车机）
    'blue': (0, 72, 255),
    'red': (244, 32, 32),  # 刹车圆：高饱和纯红
    'yellow': (251, 191, 36),
    'dim_gray': (96, 96, 96),
    'gforce_dot': (94, 233, 160),
    'banner_bg': (48, 40, 40, 160),
    'circle_gray': (100, 100, 100),  # 四角元素圆形底（对齐特斯拉车机的浅灰圆圈）
}

GEAR_TEXT = {'GEAR_PARK': 'P', 'GEAR_DRIVE': 'D', 'GEAR_REVERSE': 'R', 'GEAR_NEUTRAL': 'N'}

# 小地图定位点颜色：导航惯例蓝（高德小蓝点/特斯拉车机定位箭头同色系），
# 不用红（与刹车告警语义冲突）、不用绿（与加速 G 力语义冲突且易融入地图绿地）
MAP_DOT_BLUE = (33, 150, 243)
AP_TEXT = {'NONE': '手动', 'TACC': '主动巡航', 'AUTOSTEER': '自动转向', 'SELF_DRIVING': 'FSD',
           'AUTO_STEER': '自动转向', 'AP': 'FSD'}

# event.json 的 reason 字段翻译表（未收录的保留英文原文）
# 官方完整列表参考 Sentry-Six/src/renderer/script.js formatEventReason()
REASON_MAP = {
    # —— 手动保存 ——
    'user_interaction_dashcam': '手动保存录像',
    'user_interaction_dashcam_icon_tapped': '手动保存录像',
    'user_interaction_dashcam_panel_save': '手动保存录像',
    'user_interaction_dashcam_launcher_action_tapped': '手动保存录像',
    'user_interaction_dashcam_launcher_action_saved': '手动保存录像',  # 兜底
    # —— 鸣笛 ——
    'user_interaction_honk': '鸣笛保存',
    # —— 车辆主动安全 ——
    'vehicle_auto_emergency_braking': '紧急制动',
    'collision': '检测到碰撞',
    'vehicle_collision_detection': '检测到碰撞',  # 兜底
    # —— 哨兵模式（停车监控）——
    'sentry_aware_object_detection': '哨兵检测到物体',
    'sentry_aware_accel': '哨兵检测到震动',
    'sentry_mode': '哨兵模式触发',  # 兜底
    'sentry_mode_aware': '哨兵模式触发',  # 兜底
}


def load_event_info(video_path):
    """读取视频同目录下的 event.json，没有则返回 None"""
    event_path = os.path.join(os.path.dirname(video_path), 'event.json')
    if not os.path.exists(event_path):
        return None
    try:
        with open(event_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        reason_raw = data.get('reason', '')
        return {
            'city': data.get('city', ''),
            'street': data.get('street', ''),
            'est_lat': data.get('est_lat', ''),
            'est_lon': data.get('est_lon', ''),
            'reason_raw': reason_raw,
            'reason_cn': REASON_MAP.get(reason_raw, reason_raw),
        }
    except Exception as e:
        print(f"Warning: failed to parse {event_path}: {e}")
        return None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(SCRIPT_DIR, 'assets')

# ===== 真实地图：OSM 瓦片（方案B：缓存合成图 + 上限淘汰）=====
# 合成后的地图图缓存在脚本旁的 .map_cache，超过上限按最久未用淘汰
MAP_CACHE_DIR = os.path.join(SCRIPT_DIR, '.map_cache')
MAP_CACHE_MAX_BYTES = 200 * 1024 * 1024  # 缓存上限 200MB
MAP_ZOOM = 17                            # 缩略图缩放级别：可见街道
MAP_TILE_SIZE = 256                      # OSM 瓦片像素尺寸
# OSM 瓦片源：依次回退，提升弱网/大陆环境下取图成功率。
# 均为社区托管的 OSM 瓦片服务，遵循其使用政策（仅按需少量请求、带 UA、缓存后复用）
MAP_TILE_SOURCES = [
    'https://tile.openstreetmap.org/{z}/{x}/{y}.png',         # 官方（Fastly）
    'https://tile.openstreetmap.de/{z}/{x}/{y}.png',          # 德国镜像
    'https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png',  # 法国镜像（OSM France）
]
MAP_TILE_TIMEOUT = 5                     # 单张瓦片请求超时（秒）
MAP_USER_AGENT = 'tesla_dashcam_processor/1.0 (personal dashcam overlay tool)'
# 高德静态地图（国内直连，需自行申请免费 Key），作为优先数据源；OSM 作为后备
AMAP_STATIC_URL = 'https://restapi.amap.com/v3/staticmap'
AMAP_MAX_SIZE = 1024                     # 高德静态图单边最大像素
MAP_SOURCE_ORDER = ('amap', 'osm')       # 取图数据源优先级
MAP_ALPHA = 255  # 真实地图贴入卡片时的不透明度（0-255，255=完全不透明的原始地图）

# Sentry Six 源代码中的转向灯 SVG 路径 (从 index.html 提取)
SVG_ARROW_LEFT = 'M-13.11,-33.71 C-15.61,-33.7 -16.06,-36.09 -15.15,-38.05 C-9.81,-49.52 -5.3,-59.32 -1.63,-67.45 C1.42,-74.21 -0.76,-78.22 -5.55,-83.46 C-5.8,-83.72 -6.07,-83.94 -6.38,-84.12 C-14.17,-88.49 -20.03,-84.98 -25.77,-79.01 C-36.67,-67.69 -45.88,-58.89 -57.03,-47.37 C-62.85,-41.36 -73.29,-30.89 -88.34,-15.96 C-91.84,-12.49 -95.74,-8.94 -97.13,-4.62 C-99.07,1.36 -96.56,6.15 -92.31,10.41 C-59.34,43.5 -38.6,64.39 -30.11,73.08 C-27.75,75.49 -26.47,76.81 -26.28,77.03 C-23.64,80 -20.08,83.56 -16.05,83.93 C-13.88,84.12 -11.77,84.13 -9.72,83.94 C-9.38,83.91 -9.05,83.83 -8.73,83.7 C-1.64,80.77 1.19,75.32 -0.25,67.36 C-0.32,66.99 -0.43,66.63 -0.58,66.29 C-5.37,55.61 -9.91,45.68 -14.2,36.51 C-15.9,32.89 -13.31,31.3 -10.28,31.29 C5.65,31.24 34.48,31.23 76.21,31.24 C83.28,31.24 87.73,30.78 89.56,29.85 C93.33,27.94 96.02,24.45 97.65,19.38 C97.74,19.08 97.79,18.76 97.79,18.44 L97.73,-20.92 C97.73,-21.05 97.71,-21.18 97.68,-21.31 C96.46,-26.68 93.59,-30.44 89.07,-32.6 C87.14,-33.53 83.84,-33.99 79.19,-33.98 C43.76,-33.92 13,-33.83 -13.11,-33.71Z'

SVG_ARROW_RIGHT = 'M13.11,-33.71 C15.61,-33.7 16.06,-36.09 15.15,-38.05 C9.81,-49.52 5.3,-59.32 1.63,-67.45 C-1.42,-74.21 0.76,-78.22 5.55,-83.46 C5.8,-83.72 6.07,-83.94 6.38,-84.12 C14.17,-88.49 20.03,-84.98 25.77,-79.01 C36.67,-67.69 45.88,-58.89 57.03,-47.37 C62.85,-41.36 73.29,-30.89 88.34,-15.96 C91.84,-12.49 95.74,-8.94 97.13,-4.62 C99.07,1.36 96.56,6.15 92.31,10.41 C59.34,43.5 38.6,64.39 30.11,73.08 C27.75,75.49 26.47,76.81 26.28,77.03 C23.64,80 20.08,83.56 16.05,83.93 C13.88,84.12 11.77,84.13 9.72,83.94 C9.38,83.91 9.05,83.83 8.73,83.7 C1.64,80.77 -1.19,75.32 0.25,67.36 C0.32,66.99 0.43,66.63 0.58,66.29 C5.37,55.61 9.91,45.68 14.2,36.51 C15.9,32.89 13.31,31.3 10.28,31.29 C-5.65,31.24 -34.48,31.23 -76.21,31.24 C-83.28,31.24 -87.73,30.78 -89.56,29.85 C-93.33,27.94 -96.02,24.45 -97.65,19.38 C-97.74,19.08 -97.79,18.76 -97.79,18.44 L-97.73,-20.92 C-97.73,-21.05 -97.71,-21.18 -97.68,-21.31 C-96.46,-26.68 -93.59,-30.44 -89.07,-32.6 C-87.14,-33.53 -83.84,-33.99 -79.19,-33.98 C-43.76,-33.92 -13,-33.83 13.11,-33.71Z'

gforce_history = []
# 转向灯闪烁相位状态：以激活帧为周期起点（对齐 Sentry-Six），每个视频开始时重置
_blinker_state = {'left_prev': False, 'left_start': 0, 'right_prev': False, 'right_start': 0}
_icon_cache = {}


def parse_svg_path(path_str):
    """解析 SVG 路径，返回多边形点列表（支持 M/L/C/Z 命令）"""
    tokens = re.findall(r'[MLCZmlcz]|-?\d+\.?\d*', path_str)

    polygons = []
    current_polygon = []
    current_point = (0.0, 0.0)
    i = 0

    while i < len(tokens):
        cmd = tokens[i]
        i += 1

        if cmd in ('M', 'm'):
            x = float(tokens[i]); i += 1
            y = float(tokens[i]); i += 1
            if cmd == 'm':
                x += current_point[0]
                y += current_point[1]
            current_point = (x, y)
            if current_polygon:
                polygons.append(current_polygon)
            current_polygon = [current_point]
        elif cmd in ('L', 'l'):
            x = float(tokens[i]); i += 1
            y = float(tokens[i]); i += 1
            if cmd == 'l':
                x += current_point[0]
                y += current_point[1]
            current_point = (x, y)
            current_polygon.append(current_point)
        elif cmd in ('C', 'c'):
            x1 = float(tokens[i]); i += 1
            y1 = float(tokens[i]); i += 1
            x2 = float(tokens[i]); i += 1
            y2 = float(tokens[i]); i += 1
            x = float(tokens[i]); i += 1
            y = float(tokens[i]); i += 1
            if cmd == 'c':
                x1 += current_point[0]; y1 += current_point[1]
                x2 += current_point[0]; y2 += current_point[1]
                x += current_point[0]; y += current_point[1]
            p0 = current_point
            p1 = (x1, y1)
            p2 = (x2, y2)
            p3 = (x, y)
            for j in range(1, 21):
                t = j / 20.0
                bx = (1-t)**3 * p0[0] + 3*(1-t)**2*t * p1[0] + 3*(1-t)*t**2 * p2[0] + t**3 * p3[0]
                by = (1-t)**3 * p0[1] + 3*(1-t)**2*t * p1[1] + 3*(1-t)*t**2 * p2[1] + t**3 * p3[1]
                current_polygon.append((bx, by))
            current_point = (x, y)
        elif cmd in ('Z', 'z'):
            if current_polygon:
                current_polygon.append(current_polygon[0])

    if current_polygon:
        polygons.append(current_polygon)

    return polygons


def draw_svg_arrow(img, center_x, center_y, size, svg_path, color):
    """在指定位置绘制源代码 SVG 路径定义的箭头（按墨迹范围居中对齐）

    箭头可拆为三角形头部 + 矩形尾部：头部不动，仅水平压缩尾部矩形段，
    使箭头整体更短（对齐特斯拉车机的短粗箭头样式）。
    """
    polygons = parse_svg_path(svg_path)

    # viewBox = -100 -90 200 175，坐标范围约 -97~97
    scale_factor = size / 200.0

    # 尾部矩形段压缩：左箭头尾部在 x>-13 区域，右箭头为其镜像（x<13）
    TAIL_COMPRESS = 0.45  # 尾部长度压缩比例
    is_left = svg_path.lstrip().startswith('M-')
    compressed = []
    for poly in polygons:
        cpoly = []
        for (x, y) in poly:
            if is_left and x > -13:
                x = -13 + (x + 13) * TAIL_COMPRESS
            elif not is_left and x < 13:
                x = 13 + (x - 13) * TAIL_COMPRESS
            cpoly.append((x, y))
        compressed.append(cpoly)
    polygons = compressed

    # 箭头路径的墨迹包围盒并不以原点为中心，先算出实际包围盒中心作为偏移，
    # 保证墨迹中心精确落在 (center_x, center_y)
    all_pts = [pt for poly in polygons for pt in poly]
    if all_pts:
        min_x = min(p[0] for p in all_pts)
        max_x = max(p[0] for p in all_pts)
        min_y = min(p[1] for p in all_pts)
        max_y = max(p[1] for p in all_pts)
        ink_cx = (min_x + max_x) / 2
        ink_cy = (min_y + max_y) / 2
    else:
        ink_cx = ink_cy = 0.0

    for poly in polygons:
        scaled_poly = []
        for (x, y) in poly:
            px = center_x + (x - ink_cx) * scale_factor
            py = center_y + (y - ink_cy) * scale_factor
            scaled_poly.append((px, py))

        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.polygon(scaled_poly, fill=color)
        img.paste(overlay, (0, 0), overlay)


# ===== MP4 SEI 提取（参考 Sentry Six dashcam-mp4.js）=====

def _mp4_find_box(data, start, end, name):
    """在 MP4 二进制数据中查找指定 box，返回 (content_start, content_end)"""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack('>I', data[pos:pos + 4])[0]
        box_type = data[pos + 4:pos + 8].decode('ascii', errors='replace')
        header_size = 8
        if size == 1:
            size = struct.unpack('>Q', data[pos + 8:pos + 16])[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if box_type == name:
            return pos + header_size, pos + size
        pos += size
    return None, None


def _mp4_get_frame_durations(data):
    """从 MP4 moov 中获取帧时长列表（毫秒）"""
    moov_s, moov_e = _mp4_find_box(data, 0, len(data), 'moov')
    if moov_s is None:
        return []
    trak_s, trak_e = _mp4_find_box(data, moov_s, moov_e, 'trak')
    if trak_s is None:
        return []
    mdia_s, mdia_e = _mp4_find_box(data, trak_s, trak_e, 'mdia')
    if mdia_s is None:
        return []
    mdhd_s, mdhd_e = _mp4_find_box(data, mdia_s, mdia_e, 'mdhd')
    if mdhd_s is None:
        return []
    mdhd_ver = data[mdhd_s]
    timescale = struct.unpack('>I', data[mdhd_s + (20 if mdhd_ver == 1 else 12):mdhd_s + (24 if mdhd_ver == 1 else 16)])[0]
    minf_s, minf_e = _mp4_find_box(data, mdia_s, mdia_e, 'minf')
    if minf_s is None:
        return []
    stbl_s, stbl_e = _mp4_find_box(data, minf_s, minf_e, 'stbl')
    if stbl_s is None:
        return []
    stts_s, stts_e = _mp4_find_box(data, stbl_s, stbl_e, 'stts')
    if stts_s is None:
        return []
    entry_count = struct.unpack('>I', data[stts_s + 4:stts_s + 8])[0]
    durations = []
    pos = stts_s + 8
    for _ in range(entry_count):
        count = struct.unpack('>I', data[pos:pos + 4])[0]
        delta = struct.unpack('>I', data[pos + 4:pos + 8])[0]
        ms = (delta / timescale) * 1000 if timescale else 33.333
        for _ in range(count):
            durations.append(ms)
        pos += 8
    return durations


def _strip_emulation_bytes(data):
    """去除 H.264 emulation prevention bytes (0x03)"""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _parse_varint(data, i):
    result = 0
    shift = 0
    while i < len(data):
        byte = data[i]
        result |= (byte & 0x7F) << shift
        shift += 7
        i += 1
        if not (byte & 0x80):
            break
    return result, i


def _parse_protobuf(data):
    """手动解析 protobuf，返回 {field_number: value}"""
    fields = {}
    i = 0
    while i < len(data):
        key, i = _parse_varint(data, i)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, i = _parse_varint(data, i)
        elif wire_type == 1:
            value = struct.unpack('<d', data[i:i + 8])[0]
            i += 8
        elif wire_type == 2:
            length, i = _parse_varint(data, i)
            value = data[i:i + length]
            i += length
        elif wire_type == 5:
            value = struct.unpack('<f', data[i:i + 4])[0]
            i += 4
        elif wire_type == 7:
            value = struct.unpack('<I', data[i:i + 4])[0]
            i += 4
        else:
            break
        fields[field_number] = value
    return fields


def _decode_sei_nal(nal_data):
    """解码 SEI NAL 单元为遥测数据字典"""
    if len(nal_data) < 4:
        return None
    i = 3
    while i < len(nal_data) and nal_data[i] == 0x42:
        i += 1
    if i <= 3 or i + 1 >= len(nal_data) or nal_data[i] != 0x69:
        return None
    raw = nal_data[i + 1:len(nal_data) - 1]
    pb_data = _strip_emulation_bytes(raw)
    fields = _parse_protobuf(pb_data)
    gear_map = {0: 'GEAR_PARK', 1: 'GEAR_DRIVE', 2: 'GEAR_REVERSE', 3: 'GEAR_NEUTRAL'}
    ap_map = {0: 'NONE', 1: 'SELF_DRIVING', 2: 'AUTOSTEER', 3: 'TACC'}
    return {
        'frame_seq_no': fields.get(3, 0),
        'vehicle_speed_mps': fields.get(4, 0.0),
        'accelerator_pedal_position': fields.get(5, 0.0),
        'steering_wheel_angle': fields.get(6, 0.0),
        'blinker_on_left': bool(fields.get(7, 0)),
        'blinker_on_right': bool(fields.get(8, 0)),
        'brake_applied': bool(fields.get(9, 0)),
        'gear_state': gear_map.get(fields.get(2, 0), 'GEAR_PARK'),
        'autopilot_state': ap_map.get(fields.get(10, 0), 'NONE'),
        'latitude_deg': fields.get(11, 0.0),
        'longitude_deg': fields.get(12, 0.0),
        'heading_deg': fields.get(13, 0.0),
        'linear_acceleration_mps2_x': fields.get(14, 0.0),
        'linear_acceleration_mps2_y': fields.get(15, 0.0),
        'linear_acceleration_mps2_z': fields.get(16, 0.0),
    }


def extract_sei_from_mp4(mp4_path):
    """从 Tesla MP4 视频文件中提取 SEI 数据，返回 SEI 数据列表"""
    with open(mp4_path, 'rb') as f:
        data = f.read()

    durations = _mp4_get_frame_durations(data)

    mdat_s, mdat_e = _mp4_find_box(data, 0, len(data), 'mdat')
    if mdat_s is None:
        print("Error: mdat box not found")
        return []

    sei_data = []
    cursor = mdat_s
    end = mdat_e
    pending_sei = None
    frame_index = 0

    while cursor + 4 <= end:
        nal_size = struct.unpack('>I', data[cursor:cursor + 4])[0]
        cursor += 4
        if nal_size < 1 or cursor + nal_size > end:
            break
        nal_type = data[cursor] & 0x1F
        if nal_type == 6:  # SEI
            nal_data = data[cursor:cursor + nal_size]
            pending_sei = _decode_sei_nal(nal_data)
        elif nal_type in (5, 1):  # IDR 或 P-slice = 视频帧
            if pending_sei:
                sei = pending_sei.copy()
                sei['frame_seq_no'] = frame_index
                sei_data.append(sei)
                pending_sei = None
            else:
                sei_data.append({
                    'frame_seq_no': frame_index,
                    'vehicle_speed_mps': 0.0, 'accelerator_pedal_position': 0.0,
                    'steering_wheel_angle': 0.0, 'brake_applied': False,
                    'gear_state': 'GEAR_PARK', 'autopilot_state': 'NONE',
                    'blinker_on_left': False, 'blinker_on_right': False,
                    'latitude_deg': 0.0, 'longitude_deg': 0.0, 'heading_deg': 0.0,
                    'linear_acceleration_mps2_x': 0.0, 'linear_acceleration_mps2_y': 0.0,
                    'linear_acceleration_mps2_z': 0.0,
                })
            frame_index += 1
        cursor += nal_size

    return sei_data


def save_sei_csv(sei_data, csv_path):
    """保存 SEI 数据为 CSV 文件"""
    fields = ['frame_seq_no', 'vehicle_speed_mps', 'accelerator_pedal_position',
              'steering_wheel_angle', 'brake_applied', 'gear_state',
              'autopilot_state', 'blinker_on_left', 'blinker_on_right',
              'latitude_deg', 'longitude_deg', 'heading_deg',
              'linear_acceleration_mps2_x', 'linear_acceleration_mps2_y',
              'linear_acceleration_mps2_z']
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for sei in sei_data:
            row = {}
            for k in fields:
                v = sei.get(k, '')
                if k in ('brake_applied', 'blinker_on_left', 'blinker_on_right') and isinstance(v, bool):
                    v = 'TRUE' if v else 'FALSE'
                row[k] = v
            writer.writerow(row)


def extract_sei_from_csv(csv_path):
    sei_data = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                sei_data.append({
                    'frame_seq_no': int(row['frame_seq_no']),
                    'vehicle_speed_mps': float(row['vehicle_speed_mps']),
                    'accelerator_pedal_position': float(row['accelerator_pedal_position']),
                    'steering_wheel_angle': float(row['steering_wheel_angle']),
                    'brake_applied': row['brake_applied'] == 'TRUE',
                    'gear_state': row['gear_state'],
                    'autopilot_state': row['autopilot_state'],
                    'blinker_on_left': row['blinker_on_left'] == 'TRUE',
                    'blinker_on_right': row['blinker_on_right'] == 'TRUE',
                    'linear_acceleration_mps2_x': float(row['linear_acceleration_mps2_x']),
                    'linear_acceleration_mps2_y': float(row['linear_acceleration_mps2_y']),
                    'linear_acceleration_mps2_z': float(row['linear_acceleration_mps2_z']),
                    'latitude_deg': float(row['latitude_deg']),
                    'longitude_deg': float(row['longitude_deg']),
                    'heading_deg': float(row['heading_deg']),
                })
            except (KeyError, ValueError) as e:
                print(f"Skipping row due to error: {e}")
    return sei_data


def parse_video_timestamp(video_path):
    """从视频文件名解析原始拍摄时间，如 2026-07-05_12-51-26-front.mp4 → 2026-07-05 12:51:26"""
    filename = os.path.basename(video_path)
    name_without_ext = filename.replace('.mp4', '')
    parts = name_without_ext.split('_')
    if len(parts) >= 2:
        date_str = parts[0]
        time_str = parts[1]
        time_str = time_str[:8]
        try:
            return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H-%M-%S")
        except ValueError:
            pass
    return datetime.now()


def load_icon(icon_name, size):
    cache_key = f'{icon_name}_{size}'
    if cache_key in _icon_cache:
        return _icon_cache[cache_key].copy()
    icon_path = os.path.join(ICON_PATH, f'{icon_name}.png')
    if os.path.exists(icon_path):
        img = Image.open(icon_path).convert('RGBA')
        resized = img.resize((size, size), Image.LANCZOS)
        _icon_cache[cache_key] = resized
        return resized.copy()
    return None


def load_car_image(width):
    """按宽度等比加载侧视轿车图（assets/car_side.png，透明背景、车头朝左）。"""
    cache_key = f'car_side_{width}'
    if cache_key in _icon_cache:
        return _icon_cache[cache_key].copy()
    car_path = os.path.join(ICON_PATH, 'car_side.png')
    if os.path.exists(car_path):
        img = Image.open(car_path).convert('RGBA')
        w, h = img.size
        new_h = max(1, int(width * h / w))
        resized = img.resize((width, new_h), Image.LANCZOS)
        _icon_cache[cache_key] = resized
        return resized.copy()
    return None


def tint_icon(img, tint_color, grayscale=False):
    if img is None:
        return None
    r, g, b, a = img.split()
    if grayscale:
        # 用亮度作基底染色。注意必须保留原透明通道：
        # convert('L') 会丢弃 alpha，导致透明背景变成不透明的深色方块（图标"黑框"）
        lum = img.convert('L')
        r = g = b = lum
    tint_r, tint_g, tint_b = tint_color
    r = r.point(lambda x: int(x * tint_r / 255))
    g = g.point(lambda x: int(x * tint_g / 255))
    b = b.point(lambda x: int(x * tint_b / 255))
    return Image.merge('RGBA', (r, g, b, a))


def get_steering_glyph(size):
    """提取方向盘的白色图形（去除 PNG 自带的蓝色圆底）。

    圆形底改由代码绘制（灰色/蓝色），保证与档位、刹车、油门三个角的
    圆形底完全同尺寸同颜色（对齐特斯拉车机样式）。
    """
    cache_key = f'steer_glyph_{size}'
    if cache_key in _icon_cache:
        return _icon_cache[cache_key].copy()
    icon = load_icon('autosteer-icon', size)
    if not icon:
        return None
    r, g, b = icon.convert('RGB').split()
    # 白色图形提取：取 RGB 最小通道，阈值 180 以上视为方向盘本体（蓝色圆底被排除）
    min_rgb = ImageChops.darker(ImageChops.darker(r, g), b)
    mask = min_rgb.point(lambda v: 255 if v > 180 else 0)
    mask = ImageChops.multiply(mask, icon.split()[3])  # 与原 alpha 相乘，保留边缘平滑
    glyph = Image.new('RGBA', icon.size, (255, 255, 255, 0))
    glyph.putalpha(mask)
    _icon_cache[cache_key] = glyph
    return glyph.copy()


def colorize_icon(img, color):
    """按亮度把图标整体染成指定颜色，保留透明通道（适合原本为单色的箭头）"""
    if img is None:
        return None
    r, g, b, a = img.split()
    tint_r, tint_g, tint_b = color
    gray = img.convert('L')
    r = gray.point(lambda x: int(x * tint_r / 255))
    g = gray.point(lambda x: int(x * tint_g / 255))
    b = gray.point(lambda x: int(x * tint_b / 255))
    return Image.merge('RGBA', (r, g, b, a))


def _rainbow_color(t):
    """彩虹色：t ∈ [0,1) 对应红→橙→黄→绿→青→蓝→紫→红。饱和度 0.85 更柔和。"""
    h = t % 1.0
    r, g, b = colorsys.hsv_to_rgb(h, 0.85, 1.0)
    return (int(r * 255), int(g * 255), int(b * 255))


# 六摄像头彩虹流动感配置：
#   1  = 颜色向观者涌来（水向近处流，前视视角）
#   -1 = 颜色向远方流走（水向远处流，后视视角）
# 侧视 repeater：地面从车头侧流向车尾侧（近→远），与后视同为 -1；
# 左右几何互为镜像（mirror 参数处理）；pillar 摄像头待单独定义
RAINBOW_FLOW = {
    'front': 1,
    'back': -1,
    'left_pillar': 1,
    'right_pillar': 1,
    'left_repeater': -1,
    'right_repeater': -1,
}


def detect_camera_type(video_path):
    """从视频文件名识别摄像头类型（特斯拉行车记录仪文件名含摄像头标识）"""
    name = os.path.basename(video_path).lower()
    for cam in ('front', 'back', 'left_pillar', 'right_pillar',
                'left_repeater', 'right_repeater'):
        if cam in name:
            return cam
    return 'front'


# 彩虹物理标定：把彩虹按「米」铺在路面上（Z(y)=fH/(y-y_h)），由此自动得到
# 「近处条纹疏且流动快、远处条纹密且近乎不动」的真实透视运动。
#   horizon_ratio: 地平线所在行占画面高度的比例（梯形顶边位置）
#   fh_ratio:      fH(焦距×相机高度，单位 像素·米) ÷ 画面高度；同型号摄像头固定不变
#   period_m:      车道虚线周期（米），由 measure_dash_period 自动测量后覆盖
RAINBOW_CALIB = {
    'horizon_ratio': 0.48,
    'fh_ratio': 3.8,
    'period_m': 10.0,
    'source': 'default',
}


def _load_speed_series(csv_path):
    """读取 SEI CSV 中逐帧车速（m/s）序列"""
    vals = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try:
                vals.append(float(row['vehicle_speed_mps']))
            except (KeyError, ValueError):
                vals.append(0.0)
    return vals


def _autocorr_period(video_path, speeds, meas_fps=36.0, width=480, step=0.02):
    """在「米」域对车道虚线自相关，求真实虚线周期（米）。测量不出/置信度不足返回 None。

    原理：车辆前进时车道虚线相对画面内固定位置扫过。取画面下半部多个固定小窗口，
    逐帧统计窗口内最亮 5% 像素的平均亮度（虚线经过时变亮）；用累计里程把时间轴
    重采样到「米」域后做自相关——同一段路上各窗口的自相关峰位置相同，取平均即可
    压制噪声，峰值横坐标即虚线周期。
    """
    import numpy as np
    tmp = tempfile.mkdtemp(prefix='rainbow_meas_')
    try:
        subprocess.run(['ffmpeg', '-v', 'quiet', '-y', '-i', video_path,
                        '-vf', 'fps=%g,scale=%d:-2' % (meas_fps, width),
                        '-pix_fmt', 'gray', os.path.join(tmp, 'f_%05d.png')],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
        files = sorted(glob.glob(os.path.join(tmp, 'f_*.png')))
        if len(files) < 60:
            return None
        frames = np.stack([np.asarray(Image.open(f), dtype=np.float32) for f in files])
        n, h, w = frames.shape
        sp = np.asarray(speeds, dtype=np.float64)
        v = sp[np.clip(np.arange(n), 0, len(sp) - 1)] / meas_fps
        dist = np.cumsum(v)                       # 每帧时刻对应的累计里程
        grid = np.arange(dist[0], dist[-1], step)

        acs = []
        for y_ratio in (0.54, 0.58, 0.62, 0.66, 0.70):
            for x0_ratio, x1_ratio in ((0.02, 0.18), (0.10, 0.26), (0.14, 0.30),
                                       (0.20, 0.36), (0.24, 0.42), (0.30, 0.50)):
                sub = frames[:, int(y_ratio * h):int((y_ratio + 0.14) * h),
                             int(x0_ratio * w):int(x1_ratio * w)]
                flat = np.sort(sub.reshape(n, -1), axis=1)
                m = flat[:, -max(1, flat.shape[1] // 20):].mean(axis=1)
                mi = np.interp(grid, dist, m)
                k = int(3.0 / step)               # 3 米滑动均值，去掉光照等低频漂移
                ker = np.ones(2 * k + 1) / (2 * k + 1)
                det = mi - np.convolve(np.pad(mi, k, mode='edge'), ker, mode='valid')
                ac = np.correlate(det, det, mode='full')[len(det) - 1:]
                acs.append(ac / max(1e-9, ac[0]))
        A = np.mean(acs, axis=0)
        lo, hi = int(6 / step), int(24 / step)
        seg = A[lo:hi]
        if seg.size < 3:
            return None
        # 取「显著的局部极大」中最小者：自相关在 2P、3P 处也有峰，取最小才能得到基频周期
        sig = 0.75 * seg.max()
        best = None
        for i in range(1, len(seg) - 1):
            if seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1] and seg[i] >= sig:
                best = i
                break
        if best is None:
            return None
        period_m, strength = (best + lo) * step, float(seg[best])
        if strength < 0.15 or not 6.5 <= period_m <= 22.0:
            return None
        return period_m
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def measure_dash_period(video_path, input_csv, camera='front'):
    """自动测量本段视频所在道路的虚线周期（米）。

    结果缓存到视频同目录 JSON，重复渲染不再重复测量。前视摄像头实测；其它摄像头
    优先复用同时间戳前视视频的测量结果（同一段路），否则按车速回退国标默认值
    （高速公路 6m 线+9m 空=15m，城市道路 4m 线+6m 空=10m）。
    """
    cache_path = os.path.splitext(video_path)[0] + '_period.json'

    def _read(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                d = json.load(f)
            return float(d['period_m']), d.get('source', 'cache')
        except (OSError, ValueError, KeyError):
            return None

    cached = _read(cache_path)
    if cached:
        print(f"虚线周期: {cached[0]:.2f} m ({cached[1]}, 缓存)")
        return cached

    result = None          # (period_m, source)
    if camera == 'front':
        # 1) 复用同一路段（同时间戳前缀，如原始视频与其剪辑片段）已有的前视测量结果
        file_name = os.path.basename(video_path)
        m = re.match(r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})', file_name)
        if m:
            for p in glob.glob(os.path.join(os.path.dirname(video_path),
                                            m.group(1) + '*-front_period.json')):
                if os.path.abspath(p) == os.path.abspath(cache_path):
                    continue
                other = _read(p)
                if other and other[1] == 'measured':
                    result = (other[0], 'front')
                    break
        # 2) 否则自行测量
        if result is None:
            try:
                speeds = _load_speed_series(input_csv)
                measured = _autocorr_period(video_path, speeds)
                if measured:
                    result = (measured, 'measured')
            except Exception as e:
                print(f"Warning: 虚线周期自动测量失败 ({e})")
    else:
        # 非前视摄像头：复用同时间戳（同路段）前视摄像头的测量结果
        prefix = os.path.splitext(os.path.basename(video_path))[0].rsplit('-', 1)[0]
        for p in glob.glob(os.path.join(os.path.dirname(video_path), prefix + '-front_period.json')):
            other = _read(p)
            if other and other[1] == 'measured':
                result = (other[0], 'front')
                break

    if result:
        period_m, source = result
    else:
        try:
            speeds = _load_speed_series(input_csv)
            avg_kmh = (sum(speeds) / len(speeds)) * MPS_TO_KMH if speeds else 0.0
        except (OSError, ValueError, ZeroDivisionError):
            avg_kmh = 0.0
        period_m, source = (15.0, 'default') if avg_kmh >= 75 else (10.0, 'default')

    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump({'period_m': round(period_m, 2), 'source': source}, f)
    except OSError:
        pass
    print(f"虚线周期: {period_m:.2f} m ({source})")
    return period_m, source


# 彩虹按里程累积的流动状态（车速变化时只改变速率，位置不跳变）
_rainbow_flow = {'D': 0.0, 'last_frame': -1}


def draw_rainbow_road(img, frame_index, speed_kmh, video_width, video_height, flow_direction=1):
    """彩虹之路：辅助转向/FSD 激活时，车头前方梯形路面变为流动的彩虹色。

    flow_direction: 1=颜色向观者涌来（水向近处流，前视）；-1=颜色向远方流走（后视）

    效果对齐特斯拉车机：
    - 彩虹区域（近大远小）覆盖前方路面，顶边落在地平线，两侧斜线贴合路面轮廓、约95%高度处与画面左右竖边相交
    - 色环按「米」铺在路面上：色带间距贴合真实路面（近疏远密），流动近快远慢
    - 透明度近处不透明、远处透明（渐隐），左右边缘按行宽比例大范围幂次虚化，与远处渐隐同风格
    """
    # —— 梯形几何（贴合实测路面轮廓，网格标定 18-43-22 front 帧）——
    # 路面沥青半宽实测：48%H 处约 0.105W，60% 0.185W，70% 0.285W，80% 0.375W，90% 0.46W
    # → 近似线性增长(斜率≈0.85/H)，到 95%H 才与画面左右竖边相交，故斜线路口取 0.95H；
    # 这样梯形两侧正好压在马路牙子上，覆盖侧视（含 B 柱）彩虹所达到的路面范围
    bottom_y = video_height
    y_h = int(video_height * RAINBOW_CALIB['horizon_ratio'])  # 地平线行 = 梯形顶边
    side_start_y = int(video_height * 0.95)   # 左右斜线与左右侧边线的交点高度
    top_half_w = int(video_width * 0.105)     # 地平线处路面半宽
    cx = int(video_width * 0.465)             # 左缘基准：实测路面左缘（车在右车道，路面左缘略偏画面左）
    side_fade_ratio = 0.3  # 外侧虚化带占行半宽比例；原 0.5 使近半行全是渐变、视觉偏窄
    width_scale = 1.45     # 左侧等比例加宽系数：各行左半宽 ×1.45（牙子处 alpha≈60+）；右侧镜像左侧

    # —— 按「米」驱动：累计里程 D += 车速 × 单帧时长（车速变化时只改速率，位置不跳变）——
    ds = max(0.0, speed_kmh / MPS_TO_KMH) / FPS
    if frame_index == _rainbow_flow['last_frame'] + 1:
        _rainbow_flow['D'] += ds
    else:
        _rainbow_flow['D'] = frame_index * ds  # 重置/跳帧兜底
    _rainbow_flow['last_frame'] = frame_index
    D = _rainbow_flow['D']

    # —— 物理标定：Z(y)=fH/(y-y_h)；颜色 = ((Z + flow·D) / period) mod 1 ——
    # 色环固定铺在路面上，随车辆里程平移：近处 Z 小 → 色带疏而移动快；远处 Z 大 → 色带密而近乎不动
    period_m = max(1.0, RAINBOW_CALIB['period_m'])
    fh = RAINBOW_CALIB['fh_ratio'] * video_height

    # —— numpy 向量化：几何遮罩与车速无关，按分辨率缓存；每帧只算行颜色 ——
    import numpy as np
    H, W = video_height, video_width
    alpha = _front_rainbow_mask(W, H, y_h, bottom_y, side_start_y, top_half_w, cx, side_fade_ratio,
                                width_scale)

    rows = np.arange(H, dtype=np.float32)
    dy = rows - y_h                       # 像素到地平线的行数
    z = np.minimum(fh / np.maximum(dy, 1.0), 300.0)
    t = ((z + flow_direction * D) / period_m) % 1.0
    r1, g1, b1 = _rainbow_color_np(t)     # 每行一个颜色

    arr = np.empty((H, W, 4), dtype=np.uint8)
    arr[..., 0] = r1[:, None]
    arr[..., 1] = g1[:, None]
    arr[..., 2] = b1[:, None]
    arr[..., 3] = alpha
    rainbow_layer = Image.fromarray(arr, 'RGBA')
    img.paste(rainbow_layer, (0, 0), rainbow_layer)


_front_rainbow_cache = {}


def _front_rainbow_mask(W, H, y_h, bottom_y, side_start_y, top_half_w, cx, side_fade_ratio, width_scale=1.0):
    """前/后视梯形 alpha 遮罩（与帧无关）：近实远虚 + 左右幂次虚化，按几何参数缓存。"""
    import numpy as np
    key = (W, H, y_h, bottom_y, side_start_y, top_half_w, cx, round(side_fade_ratio, 4), round(width_scale, 4))
    mask = _front_rainbow_cache.get(key)
    if mask is not None:
        return mask
    rows = np.arange(H, dtype=np.float32)
    dy = rows - y_h
    valid = dy >= 1.0                     # 原循环 y ∈ [y_h+1, H)
    # 近处不透明、远处透明；与地平线相接处完全渐隐
    # 幂次 1.15：比原 1.3 更容易看到远处（彩虹能铺到远处的马路牙子），
    # 又比 0.9 收敛（地平线雾感约减半）；侧视/B 柱保持 1.3 不变
    span = max(1.0, bottom_y - y_h)
    base_alpha = np.where(valid, 240.0 * (np.maximum(dy, 0.0) / span) ** 1.15, 0.0)
    # 宽度随斜线自然延伸（不封顶）：斜线穿出画面后边界继续外移，使彩虹与左右侧边自然相接
    width_norm = dy / max(1.0, side_start_y - y_h)
    # 整体等比例加宽：各行左半宽统一 ×width_scale（由近及远一起外扩，越过牙子处由虚化带收边）
    row_half_w = ((top_half_w + (cx - top_half_w) * width_norm) * width_scale).astype(np.int32)
    row_left = cx - row_half_w
    # 以左侧为标准，右侧关于画面中心镜像：保留左侧覆盖，右侧补齐到与左侧等幅 → 画面中左右对称
    row_right = W - row_left
    # 左右虚化：过渡距离为「画面中心到左缘」的 side_fade_ratio 倍（左右同宽，保证严格对称），幂次 1.3
    fade_dist = np.maximum(1.0, (W * 0.5 - row_left) * side_fade_ratio)

    xs = np.broadcast_to(np.arange(W, dtype=np.float32), (H, W))
    rl = np.broadcast_to(row_left[:, None], (H, W)).astype(np.float32)
    rr = np.broadcast_to(row_right[:, None], (H, W)).astype(np.float32)
    fd = np.broadcast_to(fade_dist[:, None], (H, W))
    xlo = np.maximum(0, rl)
    xhi = np.minimum(W, rr)
    in_row = (xs >= xlo) & (xs < xhi)
    edge_dist = np.minimum(xs - rl, rr - 1 - xs)
    edge_factor = np.clip(edge_dist / fd, 0, 1) ** 1.3
    mask = np.where(in_row & valid[:, None], base_alpha[:, None] * edge_factor, 0).astype(np.uint8)
    _front_rainbow_cache[key] = mask
    return mask


def _rainbow_color_np(t):
    """向量化彩虹取色：与 _rainbow_color 相同的 HSV(h=t, s=0.85, v=1) 映射"""
    import numpy as np
    h = (t % 1.0).astype(np.float32) * 6.0
    i = np.floor(h).astype(np.int32) % 6
    f = h - np.floor(h)
    s = 0.85
    p = 1.0 - s
    q = 1.0 - s * f
    tt = 1.0 - s * (1.0 - f)
    cases = [i == 0, i == 1, i == 2, i == 3, i == 4, i == 5]
    r = np.select(cases, [1.0, q, p, p, tt, 1.0])
    g = np.select(cases, [tt, 1.0, 1.0, q, p, p])
    b = np.select(cases, [p, p, tt, 1.0, 1.0, q])
    return (r * 255).astype(np.uint8), (g * 255).astype(np.uint8), (b * 255).astype(np.uint8)


# 侧视 repeater 彩虹几何（来自用户手绘标定，均为画面比例）
# 色带：左下→右上倾斜（band_slope<0，用户红线角度）；颜色沿垂直于色带的方向
# 流动（从下往上、略偏左 = 水向远处流）。虚实渐变按用户指定：左下最实、右上最虚。
SIDE_RAINBOW = {
    'band_slope': 0.37,     # 色带倾斜幅度 |dy/dx|（色带方程 d_band = y/H + slope·x/W）
    'band_vp': 0.40,        # 消失线 d_band（马路牙子左端一带，Z 在此趋于无穷→色带极密）
    'near_z': 3.0,          # 右下角（最近处）对应的路面距离（米）
    # 马路牙子 = y/H = c0 + c1·u + c2·u²（u=x/W）。帧间路面会弯曲（直道/弯道白线位置差 ~10%），
    # 固定边界无法帧帧贴线；取保守位置：直道帧基本贴白线、不压路外树木（弯道帧会略窄）
    'curb_c0': 0.415,
    'curb_c1': 0.22,
    'curb_c2': 0.0,
    'car_top_x': 0.0,       # 车身右边缘：顶部（贴左缘，虚化可漫到车门上）
    'car_bottom_x': 0.01,   # 车身右边缘：底部
    'car_top_y': 0.12,      # 车身上端高度
    'car_fade_w': 0.07,     # 车身边缘虚化带宽（x 方向，向左漫入车门）
    'curb_fade_h': 0.08,    # 马路牙子边缘虚化带高（y 方向）
}


def draw_rainbow_road_side(img, frame_index, speed_kmh, video_width, video_height,
                           flow_direction=-1, mirror=False):
    """侧视 repeater 彩虹之路：彩虹带斜铺在车身旁的路面上。

    远近轴（用户实测观感）：地面从下往上（略偏右）流，上面是远处；
    对角线坐标 d = x/W - K·(y/H)，左下=近、右上=远；色带 ⊥ 该方向（近水平微倾），
    按 1/Z 透视近疏远密，透明度左下最实、右上最虚，随车速流动。
    flow_direction=-1（水向远处流，与后视一致）；mirror=True 用于右 repeater（水平镜像）。
    numpy 向量化实现，速度远好于逐像素。
    """
    import numpy as np
    W, H = video_width, video_height
    K = SIDE_RAINBOW['band_slope']
    d_vp = SIDE_RAINBOW['band_vp']
    d_near = 1.0 + K                                    # 右下角 d_band（最近）
    fh = SIDE_RAINBOW['near_z'] * (d_near - d_vp)       # 使右下角 Z ≈ near_z 米
    period_m = max(1.0, RAINBOW_CALIB['period_m'])

    # 累计里程（与前视共用同一状态）
    ds = max(0.0, speed_kmh / MPS_TO_KMH) / FPS
    if frame_index == _rainbow_flow['last_frame'] + 1:
        _rainbow_flow['D'] += ds
    else:
        _rainbow_flow['D'] = frame_index * ds
    _rainbow_flow['last_frame'] = frame_index
    D = _rainbow_flow['D']

    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    if mirror:  # 右 repeater：按镜像后的坐标系计算（车身在右、路面在左）
        xs = (W - 1) - xs

    # 色带坐标 d_band（等值线 = 左下→右上倾斜的色带，远处=上偏左）→ 1/Z 透视
    d = ys / H + K * (xs / W)
    Z = fh / np.maximum(d - d_vp, 0.05)
    Z = np.minimum(Z, 200.0)
    t = ((Z + flow_direction * D) / period_m) % 1.0
    r, g, bc = _rainbow_color_np(t)

    # —— 遮罩：车身边缘虚化 + 马路牙子边缘虚化 + 左下最实、右上最虚 ——
    x_car = SIDE_RAINBOW['car_top_x'] * W + (SIDE_RAINBOW['car_bottom_x'] - SIDE_RAINBOW['car_top_x']) * W \
        * np.clip((ys - SIDE_RAINBOW['car_top_y'] * H) / ((1.0 - SIDE_RAINBOW['car_top_y']) * H), 0, 1)
    fe_car = np.clip((xs - x_car) / (SIDE_RAINBOW['car_fade_w'] * W), 0, 1) ** 1.3
    y_curb = (SIDE_RAINBOW['curb_c0'] + SIDE_RAINBOW['curb_c1'] * (xs / W)
              + SIDE_RAINBOW['curb_c2'] * (xs / W) ** 2) * H
    fe_curb = np.clip((ys - y_curb) / (SIDE_RAINBOW['curb_fade_h'] * H), 0, 1) ** 1.3
    # 前实后虚：相对马路牙子（远边）的竖直渐变，与前视相对地平线渐隐同一逻辑——
    # 底边（近=前）最实(255)，沿牙子（远=后）保留 ~30% 底色（不再全透明到看不清），与左右位置无关
    nearness = np.clip((ys - y_curb) / np.maximum(H - y_curb, 1.0), 0, 1)
    alpha = (255.0 * (0.3 + 0.7 * nearness ** 0.8) * fe_car * fe_curb).astype(np.uint8)

    layer = Image.fromarray(np.dstack([r, g, bc, alpha]), 'RGBA')
    img.paste(layer, (0, 0), layer)


# B 柱（正侧视）彩虹几何：色带 = 垂直于行车方向的路面横线在透视下的像——
# 过左缘地平线一点 (fan_uc, fan_vh) 的直线束（左上→右下斜"\"），底部等距=等米；
# 控制区 = 车身旁路面带（上边界=对侧白线）。实测：左 B 柱前方在画面右、车尾在左，
# 地面内容向左流；右 B 柱水平镜像。
SIDE_PILLAR = {
    'top_y0': 0.77,      # 对侧白线左缘高度（18-43-22 片段标定：左 77%）
    'top_slope': -0.15,  # 白线向右上收（右缘 62%）
    'top_fade_h': 0.07,  # 上边界虚化带高
    'fan_uc': 0.0,       # 色带扇形中心 u（条纹延长线交汇于左缘地平线）
    'fan_vh': 0.45,      # 扇形中心 v ≈ 地平线行
    'span_m': 12.0,      # 底边可见路面纵向长度（米）：越小色带越粗；12m ≈ 0.8 个 15m 周期，
                         # 单条色带约占 21% 画宽，与前置底边色带粗细（~18% 画高）观感统一
}


def draw_rainbow_road_pillar(img, frame_index, speed_kmh, video_width, video_height,
                             flow_direction=1, mirror=False):
    """B 柱彩虹之路：扇形色带铺在车身旁路面带上（有透视、随车速流动）。

    色带为过左缘地平线的直线束：底部（近车身）等米分布，沿白线（远侧）自然收拢加密；
    前实后虚——底边最实(255)，沿白线渐隐到 0。mirror=True 用于右 B 柱。
    """
    import numpy as np
    W, H = video_width, video_height
    period_m = max(1.0, RAINBOW_CALIB['period_m'])

    # 累计里程（与前后视共用同一状态）
    ds = max(0.0, speed_kmh / MPS_TO_KMH) / FPS
    if frame_index == _rainbow_flow['last_frame'] + 1:
        _rainbow_flow['D'] += ds
    else:
        _rainbow_flow['D'] = frame_index * ds
    _rainbow_flow['last_frame'] = frame_index
    D = _rainbow_flow['D']

    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    if mirror:  # 右 B 柱：镜像坐标系（车尾在右）
        xs = (W - 1) - xs
    u = xs / W
    v = ys / H

    # 纵向米数 X_m：等值线 = 过 (fan_uc, fan_vh) 的直线束（左上→右下"\"），底边等距=等米
    uc, vh = SIDE_PILLAR['fan_uc'], SIDE_PILLAR['fan_vh']
    X_m = SIDE_PILLAR['span_m'] * (1.0 - vh) * (u - uc) / np.maximum(v - vh, 0.05)
    t = ((X_m + flow_direction * D) / period_m) % 1.0
    r, g, bc = _rainbow_color_np(t)

    # 遮罩：白线上边界虚化 + 前实后虚（无底色，沿白线收拢到 0）
    y_top = (SIDE_PILLAR['top_y0'] + SIDE_PILLAR['top_slope'] * u) * H
    fe_top = np.clip((ys - y_top) / (SIDE_PILLAR['top_fade_h'] * H), 0, 1) ** 1.3
    nearness = np.clip((ys - y_top) / np.maximum(H - y_top, 1.0), 0, 1)
    alpha = (255.0 * nearness ** 1.3 * fe_top).astype(np.uint8)

    layer = Image.fromarray(np.dstack([r, g, bc, alpha]), 'RGBA')
    img.paste(layer, (0, 0), layer)


def draw_gforce_meter(draw, center_x, center_y, size, gx, gy):
    """参考 Sentry Six gforceMeter.js + index.html SVG 设计"""
    radius = size // 2 - 2

    draw.ellipse([center_x - radius, center_y - radius,
                  center_x + radius, center_y + radius],
                 fill=(0, 0, 0, 60), outline=(255, 255, 255, 25), width=1)

    draw.ellipse([center_x - radius * 0.64, center_y - radius * 0.64,
                  center_x + radius * 0.64, center_y + radius * 0.64],
                 outline=(255, 255, 255, 15), width=1)

    draw.ellipse([center_x - radius * 0.32, center_y - radius * 0.32,
                  center_x + radius * 0.32, center_y + radius * 0.32],
                 outline=(255, 255, 255, 15), width=1)

    draw.line([center_x, center_y - radius - 2, center_x, center_y + radius + 2],
              fill=(255, 255, 255, 20), width=1)
    draw.line([center_x - radius - 2, center_y, center_x + radius + 2, center_y],
              fill=(255, 255, 255, 20), width=1)

    clamped_gx = max(-2, min(2, gx))
    clamped_gy = max(-2, min(2, gy))

    dot_x = center_x + int(clamped_gx * (radius - 8))
    dot_y = center_y - int(clamped_gy * (radius - 8))

    gforce_history.insert(0, (dot_x, dot_y))
    if len(gforce_history) > GFORCE_HISTORY_MAX:
        gforce_history.pop()

    for i, (hx, hy) in enumerate(gforce_history):
        alpha = int(255 * (0.4 - i * 0.1))
        trail_radius = int(12 - i * 3)
        if trail_radius > 0 and alpha > 0:
            draw.ellipse([hx - trail_radius, hy - trail_radius,
                          hx + trail_radius, hy + trail_radius],
                         fill=(94, 233, 160, alpha))

    if gy < -0.3:
        dot_color = COLORS['red']
    elif gy > 0.3:
        dot_color = COLORS['green']
    elif abs(gx) > 0.5:
        dot_color = COLORS['yellow']
    else:
        dot_color = COLORS['gforce_dot']

    draw.ellipse([dot_x - 12, dot_y - 12, dot_x + 12, dot_y + 12], fill=dot_color)
    draw.ellipse([dot_x - 5, dot_y - 5, dot_x + 5, dot_y + 5], fill=(255, 255, 255))


# 侧视车身（assets/car_side.png，886×315）上尾灯缝的相对坐标多边形，
# 由像素级实测得到：暗色灯罩区域 x 0.906~0.973、y 0.308~0.397，
# 形状为楔形（左尖右宽，顶边近似水平）。刹车时按此区域点亮。
CAR_TAIL_LIGHT = (
    (0.906, 0.308),  # 顶边左尖
    (0.955, 0.308),  # 顶边
    (0.973, 0.317),  # 右缘上端（楔形最宽处）
    (0.972, 0.397),  # 右下角
    (0.949, 0.397),  # 左下角
)


def draw_vertical_g_meter(img, draw, video_width, video_height, size, gz, margin, brake_applied=False):
    """右下角垂直 G 力仪表：侧视轿车（车头朝左）+ 弹簧

    车身优先使用 assets/car_side.png（透明背景 PNG、车头朝左、浅色车身），
    文件缺失时回退到代码手绘的两段式轿车（hull 扁车身 + cabin 独立座舱）。
    margin: 整车（含弹簧锚点）右缘/底缘到画面边界的距离，与左下 G 力球边距对称。
    """
    clamp_gz = max(-2.0, min(2.0, gz))
    body_color = (255, 255, 255) if abs(clamp_gz) < 1.0 else (255, 200, 60) if abs(clamp_gz) < 1.5 else (244, 32, 32)
    glass_color = (26, 26, 40)

    # —— 尺寸 ——
    body_w = int(size * 1.45)
    hull_h = max(6, int(body_w * 0.15))    # 下车身高度（扁）
    cabin_h = max(5, int(body_w * 0.13))   # 座舱高度（独立小舱）
    wheel_r = max(4, int(hull_h * 0.58))
    total_h = cabin_h + hull_h
    anchor_r = max(4, int(size * 0.06))

    # —— 位置：车身右缘距右边界 margin、弹簧锚点底部距下边界 margin ——
    cx = video_width - margin - body_w // 2
    # 锚点底部 = cy + total_h//2 + size*0.45 + anchor_r = 画面底 - margin
    cy = video_height - margin - anchor_r - int(size * 0.45) - total_h // 2

    max_shift = int(size * 0.35)
    shift = max(-max_shift, min(max_shift, int(-clamp_gz * max_shift / 2.0)))

    body_left = cx - body_w // 2
    body_right = cx + body_w // 2
    cabin_top_y = cy - total_h // 2 + shift
    hull_top_y = cabin_top_y + cabin_h
    hull_bottom_y = hull_top_y + hull_h

    # —— 层1 锚点 + 弹簧（挂在 hull 底部、两轮之间）——
    spring_bottom_y = cy + total_h // 2 + int(size * 0.45)
    spring_top_y = hull_bottom_y
    spring_actual_len = spring_bottom_y - spring_top_y
    draw.ellipse([cx - anchor_r, spring_bottom_y - anchor_r,
                  cx + anchor_r, spring_bottom_y + anchor_r],
                 fill=(80, 80, 80), outline=(120, 120, 120), width=1)
    spring_w = int(body_w * 0.18)
    spring_color = (180, 180, 180) if abs(clamp_gz) < 1.0 else (255, 200, 60) if abs(clamp_gz) < 1.5 else (244, 32, 32)
    coil_count = 6
    coil_h = spring_actual_len / (coil_count * 2) if spring_actual_len > 10 else 2
    sp_pts = []
    for i in range(coil_count * 2 + 1):
        sp_pts.append((cx - spring_w // 2 if i % 2 == 0 else cx + spring_w // 2,
                       spring_top_y + i * coil_h))
    if len(sp_pts) >= 2:
        draw.line(sp_pts, fill=spring_color, width=max(1, int(size * 0.025)))

    # —— 层2 车身：优先贴 assets/car_side.png，缺失时回退到手绘两段式轿车 ——
    car_img = tint_icon(load_car_image(body_w), body_color, grayscale=True)
    if car_img is not None:
        # 图底对齐手绘版的轮底位置（hull 底 + 轮子露出量）
        img_bottom_y = hull_bottom_y + (wheel_r * 2 // 3)
        img.paste(car_img, (cx - body_w // 2, img_bottom_y - car_img.size[1]), car_img)
        # —— 层3 刹车灯：与中央横幅刹车踏板同帧点亮，按尾灯缝实测相对坐标定位 ——
        if brake_applied:
            car_w, car_h = car_img.size
            paste_x = cx - body_w // 2
            paste_y = img_bottom_y - car_h
            pts = [(paste_x + fx * car_w, paste_y + fy * car_h) for fx, fy in CAR_TAIL_LIGHT]
            # 光晕：局部小图层画实芯后高斯模糊，垫在灯芯下面（夜间发光感）
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            pad = max(8, int(car_h * 0.12))
            lx0, ly0 = int(min(xs)) - pad, int(min(ys)) - pad
            lx1, ly1 = int(max(xs)) + pad, int(max(ys)) + pad
            glow = Image.new('RGBA', (lx1 - lx0, ly1 - ly0), (0, 0, 0, 0))
            ImageDraw.Draw(glow).polygon(
                [(x - lx0, y - ly0) for x, y in pts], fill=(255, 30, 30, 170))
            glow = glow.filter(ImageFilter.GaussianBlur(pad * 0.55))
            img.paste(glow, (lx0, ly0), glow)
            # 灯芯：楔形实芯
            draw.polygon(pts, fill=(255, 40, 40, 235))
        return

    # —— 手绘回退：下车身 hull + 座舱 + 车窗 + 轮子 + 车头灯 ——
    # 层2a 下车身 hull：长而扁的圆角矩形
    hull_rad = max(2, int(hull_h * 0.45))
    draw.rounded_rectangle([body_left, hull_top_y, body_right, hull_bottom_y],
                           radius=hull_rad, fill=body_color, outline=None)

    # —— 层3 座舱 cabin：坐在 hull 顶上的梯形（前挡风斜、车顶平、后挡风斜）——
    W = body_w
    cab_fb = body_left + int(W * 0.30)   # 前挡风底（引擎盖末端）
    cab_ft = body_left + int(W * 0.42)   # 前挡风顶（车顶前缘）
    cab_rt = body_left + int(W * 0.64)   # 后挡风顶（车顶后缘）
    cab_rb = body_left + int(W * 0.78)   # 后挡风底（尾门衔接）
    draw.polygon([(cab_fb, hull_top_y), (cab_ft, cabin_top_y),
                  (cab_rt, cabin_top_y), (cab_rb, hull_top_y)],
                 fill=body_color)

    # —— 层4 车窗：座舱内的深色前后窗（中间留 B 柱）——
    # 关键：每条斜边的水平内缩量在上下两端必须相同，
    # 窗边才是座舱斜边的严格平移（平行），否则与外轮廓明显不对齐。
    win_top = cabin_top_y + max(2, int(cabin_h * 0.28))
    win_bot = hull_top_y - max(1, int(cabin_h * 0.18))
    win_d = int(W * 0.035)          # 斜边水平内缩量（上下一致 → 平行）
    win_fb = cab_fb + win_d
    win_ft = cab_ft + win_d
    win_rt = cab_rt - win_d
    win_rb = cab_rb - win_d
    pillar_x = (win_ft + win_rt) // 2
    pillar_half = max(1, int(W * 0.015))
    # 前窗（左边缘随前挡风倾斜）
    draw.polygon([(win_fb, win_bot), (win_ft, win_top),
                  (pillar_x - pillar_half, win_top), (pillar_x - pillar_half, win_bot)],
                 fill=glass_color)
    # 后窗（右边缘随后挡风倾斜）
    draw.polygon([(pillar_x + pillar_half, win_top), (win_rt, win_top),
                  (win_rb, win_bot), (pillar_x + pillar_half, win_bot)],
                 fill=glass_color)

    # —— 层5 轮子（与 hull 下沿咬合、下半露出）——
    fwheel_cx = body_left + int(W * 0.24)
    rwheel_cx = body_left + int(W * 0.76)
    wheel_cy = hull_bottom_y - wheel_r // 3
    for wx in (fwheel_cx, rwheel_cx):
        draw.ellipse([wx - wheel_r, wheel_cy - wheel_r,
                      wx + wheel_r, wheel_cy + wheel_r],
                     fill=(22, 22, 22), outline=(65, 65, 65), width=1)
        hr = max(1, int(wheel_r * 0.35))
        draw.ellipse([wx - hr, wheel_cy - hr, wx + hr, wheel_cy + hr], fill=(58, 58, 58))

    # —— 层6 车头灯（hull 左端小椭圆）——
    hl_w = max(3, int(hull_h * 0.35))
    hl_h = max(2, int(hull_h * 0.25))
    hl_x = body_left + hl_w
    hl_y = hull_top_y + hull_h // 2
    draw.ellipse([hl_x - hl_w, hl_y - hl_h, hl_x, hl_y + hl_h],
                 fill=(255, 252, 220))


# ===== 真实地图：OSM 瓦片获取与缓存（方案B）=====

_map_image_mem_cache = {}
# 记录每个坐标地图实际使用的数据源，供署名显示（'amap' / 'osm'）
_map_source_used = {}
# 已验证可用的瓦片源（记住它，后续瓦片优先使用，避免反复等待不可用的源）
_active_tile_source = {'url': None}


def _latlon_to_tile_xy(lat, lon, zoom):
    """经纬度 → Web Mercator 瓦片浮点坐标"""
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


# ===== 坐标系转换：WGS-84（特斯拉 GPS）→ GCJ-02（高德/国内地图）=====
def _out_of_china(lat, lon):
    """粗略判断是否在中国境外（境外不加密）"""
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def wgs84_to_gcj02(lat, lon):
    """WGS-84 转 GCJ-02（火星坐标）。中国境外原样返回。
    特斯拉记录的是 WGS-84，高德使用 GCJ-02，不转换会导致位置偏移约几百米。"""
    if _out_of_china(lat, lon):
        return lat, lon
    a = 6378245.0               # 克拉索夫斯基椭球长半轴
    ee = 0.00669342162296594323  # 偏心率平方

    def _transform_lat(x, y):
        ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
               + 0.2 * math.sqrt(abs(x)))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return ret

    def _transform_lon(x, y):
        ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x)))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return ret

    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lat + dlat, lon + dlon


def _get_amap_key():
    """读取高德 Key：优先环境变量 AMAP_KEY，其次脚本目录下 amap_key.txt。
    开源分发时不要把 Key 写进代码，否则会被滥用。"""
    key = os.environ.get('AMAP_KEY', '').strip()
    if key:
        return key
    key_file = os.path.join(SCRIPT_DIR, 'amap_key.txt')
    if os.path.exists(key_file):
        try:
            with open(key_file, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except OSError:
            return ''
    return ''


def _fetch_amap_image(lat, lon, map_w, map_h):
    """从高德静态地图取图。未配置 Key 或失败返回 None。
    高德使用 GCJ-02 且 location 参数为「经度,纬度」。"""
    key = _get_amap_key()
    if not key:
        return None
    gcj_lat, gcj_lon = wgs84_to_gcj02(lat, lon)
    # 高德单边最大 1024px：超出则按比例缩小请求，再放大到目标尺寸
    req_w, req_h = map_w, map_h
    scale_back = 1.0
    if max(req_w, req_h) > AMAP_MAX_SIZE:
        scale_back = max(req_w, req_h) / float(AMAP_MAX_SIZE)
        req_w = int(req_w / scale_back)
        req_h = int(req_h / scale_back)
    params = urllib.parse.urlencode({
        'location': f'{gcj_lon:.6f},{gcj_lat:.6f}',
        'zoom': 17,
        'size': f'{req_w}*{req_h}',
        'scale': 1,
        'key': key,
    })
    url = f'{AMAP_STATIC_URL}?{params}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': MAP_USER_AGENT})
        with urllib.request.urlopen(req, timeout=MAP_TILE_TIMEOUT * 2) as resp:
            ctype = resp.headers.get('Content-Type', '')
            data = resp.read()
        if 'image' not in ctype:
            # 高德出错时返回 JSON（如 Key 无效/超额），打印便于排查
            print(f"Warning: 高德返回非图片内容: {data[:160]!r}")
            return None
        img = Image.open(BytesIO(data)).convert('RGB')
        if (req_w, req_h) != (map_w, map_h):
            img = img.resize((map_w, map_h), Image.LANCZOS)
        return img
    except Exception as e:
        print(f"Warning: 高德取图失败: {e}")
        return None


def _fetch_tile(z, x, y):
    """下载单张 OSM 瓦片：主源优先，失败则依次回退其它镜像，每个源尝试一次。
    成功后会记住可用源，后续瓦片优先使用它，避免反复等待不可用的源。"""
    sources = list(MAP_TILE_SOURCES)
    active = _active_tile_source['url']
    if active in sources:
        sources.remove(active)
        sources.insert(0, active)

    last_err = None
    for url_tpl in sources:
        url = url_tpl.format(z=z, x=x, y=y)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': MAP_USER_AGENT})
            with urllib.request.urlopen(req, timeout=MAP_TILE_TIMEOUT) as resp:
                data = resp.read()
            if _active_tile_source['url'] != url_tpl:
                _active_tile_source['url'] = url_tpl
                if url_tpl != MAP_TILE_SOURCES[0]:
                    print(f"Map: 主源不可用，改用镜像 {url_tpl.split('/')[2]}")
            return Image.open(BytesIO(data)).convert('RGB')
        except Exception as e:
            last_err = e
    raise last_err


def _compose_map_image(lat, lon, map_w, map_h, zoom):
    """按经纬度拼接 OSM 瓦片，返回以该坐标为中心、map_w×map_h 的 RGB 图"""
    cx, cy = _latlon_to_tile_xy(lat, lon, zoom)
    x0 = int(math.floor(cx - (map_w / 2.0) / MAP_TILE_SIZE))
    x1 = int(math.floor(cx + (map_w / 2.0) / MAP_TILE_SIZE))
    y0 = int(math.floor(cy - (map_h / 2.0) / MAP_TILE_SIZE))
    y1 = int(math.floor(cy + (map_h / 2.0) / MAP_TILE_SIZE))

    n = 2 ** zoom
    canvas = Image.new('RGB', ((x1 - x0 + 1) * MAP_TILE_SIZE,
                               (y1 - y0 + 1) * MAP_TILE_SIZE))
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            tile = _fetch_tile(zoom, tx % n, min(max(ty, 0), n - 1))
            canvas.paste(tile, ((tx - x0) * MAP_TILE_SIZE, (ty - y0) * MAP_TILE_SIZE))

    off_x = round((cx - x0) * MAP_TILE_SIZE - map_w / 2.0)
    off_y = round((cy - y0) * MAP_TILE_SIZE - map_h / 2.0)
    return canvas.crop((off_x, off_y, off_x + map_w, off_y + map_h))


def _map_cache_path(lat, lon, zoom, map_w, map_h):
    key = f"{lat:.5f}_{lon:.5f}_{zoom}_{map_w}x{map_h}"
    return os.path.join(MAP_CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + '.png')


def _evict_map_cache():
    """缓存超过上限时，按访问时间从旧到新删除，直到不超过上限"""
    try:
        entries = []
        for name in os.listdir(MAP_CACHE_DIR):
            p = os.path.join(MAP_CACHE_DIR, name)
            if os.path.isfile(p):
                st = os.stat(p)
                entries.append((st.st_mtime, st.st_size, p))
    except OSError:
        return
    total = sum(e[1] for e in entries)
    if total <= MAP_CACHE_MAX_BYTES:
        return
    entries.sort()  # 旧 → 新
    for _, size, p in entries:
        if total <= MAP_CACHE_MAX_BYTES:
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


def clear_map_cache():
    """清空地图瓦片缓存（内存 + 磁盘），返回删除的文件数"""
    _map_image_mem_cache.clear()
    count = 0
    try:
        for name in os.listdir(MAP_CACHE_DIR):
            p = os.path.join(MAP_CACHE_DIR, name)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    count += 1
                except OSError:
                    pass
    except OSError:
        pass
    return count


def _write_map_source(path, source):
    """把数据源名写入 sidecar（<hash>.png.src），跨运行保留署名信息"""
    try:
        with open(path + '.src', 'w', encoding='utf-8') as f:
            f.write(source)
    except OSError:
        pass


def _read_map_source(path):
    try:
        with open(path + '.src', 'r', encoding='utf-8') as f:
            return f.read().strip() or 'osm'
    except OSError:
        return 'osm'


def _build_map_image(lat, lon, map_w, map_h):
    """按 MAP_SOURCE_ORDER 依次尝试数据源，返回 (image, source)；全失败返回 (None, None)。
    高德仅覆盖中国境内，境外点自动跳到 OSM。"""
    for source in MAP_SOURCE_ORDER:
        if source == 'amap':
            if _out_of_china(lat, lon):
                continue
            print(f"Fetching map (高德) for ({lat}, {lon}) ...")
            img = _fetch_amap_image(lat, lon, map_w, map_h)
            if img is not None:
                return img, 'amap'
            print("Warning: 高德取图失败，改用 OSM ...")
        elif source == 'osm':
            print(f"Fetching map (OSM) for ({lat}, {lon}) ...")
            try:
                return _compose_map_image(lat, lon, map_w, map_h, MAP_ZOOM), 'osm'
            except Exception as e:
                print(f"Warning: OSM 取图失败: {e}")
    return None, None


def get_map_image(lat, lon, map_w, map_h):
    """取地图图：内存缓存 → 磁盘缓存 → 在线取图（高德优先，OSM 后备）。
    任一步失败返回 None，由调用方回退到伪路网。"""
    mem_key = (round(lat, 5), round(lon, 5), map_w, map_h)
    if mem_key in _map_image_mem_cache:
        return _map_image_mem_cache[mem_key]

    path = _map_cache_path(lat, lon, MAP_ZOOM, map_w, map_h)
    image = None
    if os.path.exists(path):
        try:
            image = Image.open(path).convert('RGB')
            os.utime(path, None)  # 命中缓存刷新 mtime，供 LRU 淘汰
            _map_source_used[mem_key] = _read_map_source(path)
        except Exception:
            image = None
    if image is None:
        image, source = _build_map_image(lat, lon, map_w, map_h)
        if image is None:
            # 把失败结果也记入内存缓存：保证同一段视频统一回退伪路网，
            # 且不会因为后续帧再次尝试取图而中途切换成真实地图
            _map_image_mem_cache[mem_key] = None
            return None
        try:
            os.makedirs(MAP_CACHE_DIR, exist_ok=True)
            image.save(path)
            _write_map_source(path, source)
            _map_source_used[mem_key] = source
            _evict_map_cache()
        except OSError:
            pass
    _map_image_mem_cache[mem_key] = image
    return image


def get_map_source(lat, lon, map_w, map_h):
    """返回该坐标地图使用的数据源名（'amap'/'osm'），未知按 'osm' 处理"""
    return _map_source_used.get((round(lat, 5), round(lon, 5), map_w, map_h), 'osm')


def prefetch_map_image(event, video_width, video_height):
    """渲染前一次性取好地图，锁定本视频统一使用真实地图还是回退伪路网。

    若在逐帧渲染时才按需取图，首帧取图失败会先画伪路网、后续帧取图成功又变成真图，
    成片开头就会出现一瞬间的"假地图"。这里提前取一次并锁定结果，避免中途切换。
    """
    if not event:
        return
    try:
        lat, lon = float(event['est_lat']), float(event['est_lon'])
    except (ValueError, TypeError, KeyError):
        return
    map_w = int(video_width * 0.30)
    map_h = int(video_height * 0.17)
    key = (round(lat, 5), round(lon, 5), map_w, map_h)

    image = get_map_image(lat, lon, map_w, map_h)
    if image is None:
        # 取图失败：把失败结果也记下，保证整段视频统一回退，不再逐帧重试
        _map_image_mem_cache[key] = None
        print("Map: 取图失败，本段视频回退伪路网")
    else:
        source_name = '高德地图' if get_map_source(lat, lon, map_w, map_h) == 'amap' else 'OSM 地图'
        print(f"Map: 使用 {source_name} ({map_w}x{map_h})")


def draw_minimap(img, draw, event, video_width, video_height, scale, font_map, font_map_small, frame_index=0):
    """GTA 风格缩略地图，显示在右上角，与左上角信息卡同尺寸对称。
    不套外框：地图直接铺满整个圆角区域（位置/大小/圆角与左卡一致）。
    优先高德静态图，其次 OSM；取图失败时回退为经纬度种子生成的伪路网。"""
    # —— 几何：与左上角信息卡严格对称（同宽同高同圆角），但无外框 ——
    card_margin = int(40 * scale)
    map_w = int(video_width * 0.30)
    map_h = int(video_height * 0.17)
    mx, my = video_width - card_margin - map_w, card_margin
    corner_r = round(map_h * 0.13)

    # 地图单独图层：圆角遮罩直接铺满卡片区域，合成到画面上
    layer = Image.new('RGBA', (map_w, map_h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    # 优先贴真实地图（高德/OSM）；取图失败自动回退到伪路网
    real_map = None
    map_source = 'osm'
    try:
        lat, lon = float(event['est_lat']), float(event['est_lon'])
        real_map = get_map_image(lat, lon, map_w, map_h)
    except (ValueError, TypeError, KeyError):
        lat = lon = None
        real_map = None

    # 底部信息条（坐标 + 数据源署名），半透明黑底保证文字可读
    strip_h = max(16, int(24 * scale))

    if real_map is not None:
        if lat is not None:
            map_source = get_map_source(lat, lon, map_w, map_h)
        rm = real_map.convert('RGBA')
        rm.putalpha(MAP_ALPHA)  # 地图半透明，视频画面可微微透出，与其他两卡风格统一
        layer.paste(rm, (0, 0), rm)
    else:
        # —— 回退：用经纬度做种子生成确定性的伪路网（同一事件每次画出来一样）——
        ld.rectangle([0, 0, map_w - 1, map_h - 1], fill=(12, 18, 24, 235))
        try:
            seed = int(float(event['est_lat']) * 1000) + int(float(event['est_lon']) * 1000)
        except (ValueError, TypeError):
            seed = 42
        rng = (seed * 2654435761) % (2 ** 32)

        def next_rand():
            nonlocal rng
            rng = (rng * 1103515245 + 12345) % (2 ** 31)
            return rng / (2 ** 31)

        road_color = (70, 82, 92, 255)
        road_w = max(2, int(3.5 * scale))
        cx0, cy0 = map_w // 2, (map_h - strip_h) // 2
        # 主路：斜向穿过中心红点
        angle = next_rand() * math.pi
        dx, dy = math.cos(angle), math.sin(angle)
        main_len = min(map_w, map_h - strip_h) * 0.7
        ld.line([max(4, cx0 - dx * main_len), max(4, cy0 - dy * main_len),
                 min(map_w - 4, cx0 + dx * main_len), min(map_h - strip_h - 4, cy0 + dy * main_len)],
                fill=(96, 110, 122, 255), width=road_w + max(1, int(1.5 * scale)))
        # 次级街道网格
        for _ in range(3):
            y_off = (next_rand() - 0.5) * (map_h - strip_h) * 0.6
            ry = cy0 + y_off
            if int(14 * scale) < ry < map_h - strip_h - int(10 * scale):
                ld.line([int(6 * scale), int(ry), map_w - int(6 * scale), int(ry)],
                        fill=road_color, width=road_w)
            x_off = (next_rand() - 0.5) * map_w * 0.6
            rx = cx0 + x_off
            if int(6 * scale) < rx < map_w - int(6 * scale):
                ld.line([int(rx), int(14 * scale), int(rx), map_h - strip_h - int(6 * scale)],
                        fill=road_color, width=road_w)

    # 底部信息条
    ld.rectangle([0, map_h - strip_h, map_w, map_h], fill=(0, 0, 0, 120))

    # 中心定位点（当前位置）+ 雷达涟漪
    # 严格落在卡片几何中心：高德/OSM 均以定位点为中心渲染该尺寸的地图，
    # 地图铺满卡片后其图心即定位点，定位点必须与图心重合才算位置准确
    cx0, cy0 = map_w // 2, map_h // 2
    dot_r = max(5, int(7 * scale))

    # —— 雷达涟漪：两圈圆环从定位点边缘扩散、渐隐，营造「发射信号」感 ——
    # 画在独立图层再 alpha_composite，使涟漪正确混入地图（直接画半透明色会
    # 替换像素 alpha，导致透出视频背景而发黑）
    ripple = Image.new('RGBA', (map_w, map_h), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ripple)
    ripple_period = 54          # 1.5 秒 @36fps
    ripple_max_r = dot_r * 3.2  # 扩散最大半径
    for phase_off in (0, ripple_period // 2):  # 双环错开半周期，扩散更连续
        phase = ((frame_index + phase_off) % ripple_period) / ripple_period
        rr = dot_r + phase * (ripple_max_r - dot_r)
        alpha = int((1.0 - phase) * 150)
        if alpha > 0:
            w = max(1, int(2 * scale))
            rd.ellipse([cx0 - rr, cy0 - rr, cx0 + rr, cy0 + rr],
                       outline=MAP_DOT_BLUE + (alpha,), width=w)
    layer = Image.alpha_composite(layer, ripple)
    ld = ImageDraw.Draw(layer)

    # —— 定位点本体：实心蓝点 + 白描边（导航惯例：高德小蓝点/特斯拉定位箭头）——
    ld.ellipse([cx0 - dot_r, cy0 - dot_r, cx0 + dot_r, cy0 + dot_r],
               fill=MAP_DOT_BLUE, outline=(255, 255, 255), width=max(1, int(2 * scale)))

    # 信息条左侧：经纬度小字；右侧：OSM 署名
    # （高德静态图自带「高德地图」水印署名，无需再画；用 ASCII (c) 避免黑体缺 © 字形）
    ld.text((int(10 * scale), map_h - strip_h / 2 - int(scale)),
            f"{event['est_lat']}, {event['est_lon']}",
            font=font_map_small, fill=COLORS['dim_white'], anchor='lm')
    if real_map is not None and map_source == 'osm':
        ld.text((map_w - int(8 * scale), map_h - strip_h / 2 - int(scale)),
                '(c) OpenStreetMap', font=font_map_small,
                fill=COLORS['dim_white'], anchor='rm')

    # 右上角指北针
    nx = map_w - int(22 * scale)
    ny = int(18 * scale)
    nr = int(9 * scale)
    ld.polygon([(nx, ny - nr), (nx - nr * 0.6, ny + nr), (nx + nr * 0.6, ny + nr)],
               fill=COLORS['red'])
    ld.text((nx, ny + nr + int(6 * scale)), 'N', font=font_map_small,
            fill=COLORS['dim_white'], anchor='ma')

    # 圆角遮罩：把地图四角裁成与左卡一致的圆角后合成（无外框）
    mask = Image.new('L', (map_w, map_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, map_w - 1, map_h - 1],
                                           radius=corner_r, fill=255)
    layer.putalpha(ImageChops.multiply(layer.getchannel('A'), mask))
    img.paste(layer, (mx, my), layer)


def generate_overlay_frame(sei, frame_index, output_dir, base_time, video_width=1920, video_height=1080, use_metric=True, event=None, camera='front'):
    img = Image.new('RGBA', (video_width, video_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ===== 彩虹之路：辅助转向/FSD 激活时绘制（最底层，先于其他叠加元素）=====
    # 流动方向由摄像头类型决定（见 RAINBOW_FLOW：前视水向近处流，后视水向远处流）
    ap_state = sei['autopilot_state']
    if ap_state in ('AUTOSTEER', 'SELF_DRIVING'):
        speed_kmh = sei['vehicle_speed_mps'] * MPS_TO_KMH
        if camera in ('left_repeater', 'right_repeater'):
            draw_rainbow_road_side(img, frame_index, speed_kmh, video_width, video_height,
                                   flow_direction=RAINBOW_FLOW.get(camera, -1),
                                   mirror=(camera == 'right_repeater'))
        elif camera in ('left_pillar', 'right_pillar'):
            draw_rainbow_road_pillar(img, frame_index, speed_kmh, video_width, video_height,
                                     flow_direction=RAINBOW_FLOW.get(camera, 1),
                                     mirror=(camera == 'right_pillar'))
        else:
            draw_rainbow_road(img, frame_index, speed_kmh, video_width, video_height,
                              flow_direction=RAINBOW_FLOW.get(camera, 1))

    scale = video_width / 1920.0

    # ===== 尺寸：对齐特斯拉车机三卡布局比例 =====
    # 左卡 ~30%宽，居中卡 ~27%宽（偏长方形），高度均为屏幕 17%
    dash_width = int(video_width * 0.27)
    dash_height_5col = int(video_height * 0.17)
    info_width = int(video_width * 0.30)

    # 字号：按三行高度分配，速度最大、单位/AP次之
    row_h = dash_height_5col / 3
    size_num = max(12, int(row_h * 1.35))  # 速度数字加大
    size_unit = max(10, int(row_h * 0.45))
    size_info = max(10, int(row_h * 0.55))
    size_ap = max(10, int(row_h * 0.42))
    size_gear = max(10, int(row_h * 0.55))  # 档位字母在圆圈内，字号需小于圆径

    try:
        font_num = ImageFont.truetype('simhei.ttf', size_num)
        font_unit = ImageFont.truetype('simhei.ttf', size_unit)
        font_info = ImageFont.truetype('simhei.ttf', size_info)
        font_ap = ImageFont.truetype('simhei.ttf', size_ap)
        font_gear = ImageFont.truetype('simhei.ttf', size_gear)
        font_gforce = ImageFont.truetype('simhei.ttf', int(14 * scale))
        font_map = ImageFont.truetype('simhei.ttf', int(34 * scale))
        font_map_small = ImageFont.truetype('simhei.ttf', int(20 * scale))
    except:
        font_num = ImageFont.load_default()
        font_unit = ImageFont.load_default()
        font_info = ImageFont.load_default()
        font_ap = ImageFont.load_default()
        font_gear = ImageFont.load_default()
        font_gforce = ImageFont.load_default()
        font_map = ImageFont.load_default()
        font_map_small = ImageFont.load_default()

    # 时间 = 文件名时间 + 帧偏移
    elapsed_time = timedelta(seconds=frame_index / FPS)
    current_time = base_time + elapsed_time
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    date_str = current_time.strftime('%Y-%m-%d')
    weekday_str = weekdays[current_time.weekday()]
    time_str = current_time.strftime('%H:%M:%S')

    # ===== 左上角双行信息条 =====
    info_x = int(40 * scale)
    info_y = int(40 * scale)
    info_h = dash_height_5col  # 和居中条同高
    corner_r = round(info_h * 0.13)  # 圆角偏小，更接近直角
    draw.rounded_rectangle([info_x, info_y, info_x + info_width, info_y + info_h],
                            radius=corner_r, fill=COLORS['banner_bg'])
    # 第一行：城市 + 街道（无 event.json 时占位）
    if event and (event['city'] or event['street']):
        location_text = ' · '.join(t for t in [event['city'], event['street']] if t)
        loc_color = COLORS['white']
    else:
        location_text = '未知城市 · 未知街道'
        loc_color = COLORS['dim_white']
    datetime_str = f"{date_str} {weekday_str} {time_str}"
    # 第三行：触发原因（无 event.json 时同样占位，保证始终三行对齐）
    if event and event.get('reason_cn'):
        reason_text = event['reason_cn']
        reason_color = COLORS['white']
    else:
        reason_text = '未知触发原因'
        reason_color = COLORS['dim_white']
    # 三行布局：地点 / 日期时间 / 触发原因
    draw.text((info_x + int(20 * scale), info_y + int(info_h * 0.2)),
              location_text, font=font_info, fill=loc_color, anchor='lm')
    draw.text((info_x + int(20 * scale), info_y + int(info_h * 0.5)),
              datetime_str, font=font_info, fill=COLORS['dim_white'], anchor='lm')
    draw.text((info_x + int(20 * scale), info_y + int(info_h * 0.8)),
              reason_text, font=font_info, fill=reason_color, anchor='lm')

    # ===== 居中条：5 列对称布局 =====
    # 最左(档位+刹车) | 中左(左转) | 中间(速度+单位+AP状态) | 中右(右转) | 最右(方向盘+油门)
    pos_x = video_width // 2
    pos_y = int(40 * scale + dash_height_5col / 2)
    dash_x = pos_x - dash_width // 2
    dash_y = pos_y - dash_height_5col // 2

    corner_r2 = round(dash_height_5col * 0.13)  # 圆角偏小，更接近直角
    draw.rounded_rectangle([dash_x, dash_y, dash_x + dash_width, dash_y + dash_height_5col],
                            radius=corner_r2, fill=COLORS['banner_bg'])

    speed = sei['vehicle_speed_mps'] * (MPS_TO_KMH if use_metric else MPS_TO_MPH)
    speed_unit = 'km/h' if use_metric else 'MPH'
    gear_text = GEAR_TEXT.get(sei['gear_state'], 'P')
    ap_text = AP_TEXT.get(ap_state, '手动')
    ap_color = COLORS['blue'] if ap_state != 'NONE' else COLORS['dim_white']

    left_blinker = sei['blinker_on_left']
    right_blinker = sei['blinker_on_right']

    # 转向灯闪烁相位
    if left_blinker and not _blinker_state['left_prev']:
        _blinker_state['left_start'] = frame_index
    if right_blinker and not _blinker_state['right_prev']:
        _blinker_state['right_start'] = frame_index
    _blinker_state['left_prev'] = left_blinker
    _blinker_state['right_prev'] = right_blinker
    left_blink_visible = left_blinker and (frame_index - _blinker_state['left_start']) % BLINKER_CYCLE_FRAMES < BLINKER_ON_FRAMES
    right_blink_visible = right_blinker and (frame_index - _blinker_state['right_start']) % BLINKER_CYCLE_FRAMES < BLINKER_ON_FRAMES
    accel_value = sei['accelerator_pedal_position']
    steering_angle = sei['steering_wheel_angle']

    # 5 列位置（等间距）
    col_padding = dash_width * 0.08
    col_usable = dash_width - col_padding * 2
    col_spacing = col_usable / 4  # 5 列 = 4 个间距
    col_positions = [dash_x + col_padding + col_spacing * i for i in range(5)]

    icon_size = int(48 * scale)
    arrow_size = int(44 * scale)  # 转向箭头比对官方样式缩短，避免细长
    # 三行布局：上(1/4) 中(1/2) 下(3/4)
    row_top = pos_y - dash_height_5col * 0.25
    row_mid = pos_y
    row_bot = pos_y + dash_height_5col * 0.25
    # 刹车/油门踏板上移，缩小与档位/方向盘的纵向间距（对齐特斯拉车机）
    row_pedal = pos_y + dash_height_5col * 0.13
    # 转向箭头垂直位置：档位/方向盘(上行)与刹车/油门(踏板行)的中点
    row_arrow = (row_top + row_pedal) / 2

    # --- 四角圆形底：对齐特斯拉车机——档位/刹车/方向盘/油门外均套同尺寸同颜色的圆圈 ---
    circle_r = icon_size / 2

    def draw_corner_circle(cx, cy, active=False, active_color=None):
        # 仅方向盘/刹车在激活时变色（方向盘蓝、刹车红），其余恒为浅灰（与特斯拉一致）
        color = (active_color or COLORS['blue']) if active else COLORS['circle_gray']
        draw.ellipse([cx - circle_r, cy - circle_r, cx + circle_r, cy + circle_r], fill=color)

    # --- 最左列：档位(上) + 刹车(下) ---
    draw_corner_circle(col_positions[0], row_top)
    # 档位字母按实际墨迹范围居中（anchor='mm' 按字宽居中，SimHei 的 D 视觉偏左），
    # stroke_width 加粗对齐官方字重。两次绘制都必须用 anchor='mm'：
    # 先在 (0,0) 测出墨迹中心偏移，再在目标点减去偏移绘制
    gb = draw.textbbox((0, 0), gear_text, font=font_gear, stroke_width=2, anchor='mm')
    gox = (gb[0] + gb[2]) / 2
    goy = (gb[1] + gb[3]) / 2
    draw.text((col_positions[0] - gox, row_top - goy),
              gear_text, font=font_gear, fill=COLORS['white'], anchor='mm',
              stroke_width=2, stroke_fill=COLORS['white'])
    brake_icon = load_icon('pedal_brake', icon_size)
    if brake_icon:
        # 刹车激活时：圆圈变红、踏板图标变白（未激活：灰圈+浅灰图标）
        draw_corner_circle(col_positions[0], row_pedal,
                           active=sei['brake_applied'], active_color=COLORS['red'])
        if sei['brake_applied']:
            tinted_brake = tint_icon(brake_icon, COLORS['white'])
        else:
            tinted_brake = tint_icon(brake_icon, COLORS['dim_white'], grayscale=True)
        img.paste(tinted_brake,
                  (int(col_positions[0] - icon_size / 2),
                   int(row_pedal - icon_size / 2)),
                  tinted_brake)

    # 转向箭头横向位置：固定按两位数的宽度居中于"内侧圆圈边缘 ↔ 时速数字边缘"之间，
    # 不随实际时速的位数变化（左箭头：档位圆右缘 ↔ 数字左缘；右箭头：数字右缘 ↔ 方向盘圆左缘）
    sb = draw.textbbox((0, 0), '00', font=font_num, stroke_width=2, anchor='mm')
    speed_half_w = (sb[2] - sb[0]) / 2
    speed_left_edge = col_positions[2] - speed_half_w
    speed_right_edge = col_positions[2] + speed_half_w
    left_arrow_x = (col_positions[0] + circle_r + speed_left_edge) / 2
    right_arrow_x = (col_positions[4] - circle_r + speed_right_edge) / 2

    # --- 中左列：左转向灯（位于档位与刹车的垂直中点） ---
    left_color = COLORS['green'] if left_blink_visible else COLORS['dim_white']
    draw_svg_arrow(img, int(left_arrow_x), int(row_arrow), arrow_size, SVG_ARROW_LEFT, left_color)

    # --- 中间列：速度(上) + 单位(中下) + AP状态(下) ---
    # km/h 墨迹底部与刹车/油门踏板圆底部对齐，stroke_width 加粗
    pedal_bottom = row_pedal + circle_r
    ub = draw.textbbox((0, 0), speed_unit, font=font_unit, anchor='mm')
    unit_y = pedal_bottom - ub[3]
    draw.text((int(col_positions[2]), int(unit_y)),
              speed_unit, font=font_unit, fill=COLORS['dim_white'], anchor='mm',
              stroke_width=1, stroke_fill=COLORS['dim_white'])
    # 速度数字：在 banner 顶边与 km/h 墨迹顶之间向下偏移，
    # 使数字顶部到框边的距离 > 数字底部到 km/h 的距离（数字更贴近 km/h）
    kmh_ink_top = unit_y + ub[1]
    speed_y = (dash_y + kmh_ink_top) / 2 + dash_height_5col * 0.08
    draw.text((int(col_positions[2]), int(speed_y)),
              f"{int(speed)}", font=font_num, fill=COLORS['white'], anchor='mm',
              stroke_width=2, stroke_fill=COLORS['white'])
    # AP 状态文字：垂直居中于 km/h 底部与 banner 底边之间，避免与 km/h 重叠
    ap_row = (pedal_bottom + dash_y + dash_height_5col) / 2
    draw.text((int(col_positions[2]), int(ap_row)),
              ap_text, font=font_ap, fill=ap_color, anchor='mm')

    # --- 中右列：右转向灯（位于方向盘与油门的垂直中点） ---
    right_color = COLORS['green'] if right_blink_visible else COLORS['dim_white']
    draw_svg_arrow(img, int(right_arrow_x), int(row_arrow), arrow_size, SVG_ARROW_RIGHT, right_color)

    # --- 最右列：方向盘(上) + 油门(下) ---
    ap_active = ap_state in ('AUTOSTEER', 'SELF_DRIVING')
    draw_corner_circle(col_positions[4], row_top, active=ap_active)
    # 方向盘只贴白色图形（原 PNG 蓝色圆底已去除，圆底由上面代码绘制以统一四角样式）
    steer_glyph = get_steering_glyph(icon_size)
    if steer_glyph:
        rotated_steer = steer_glyph.rotate(-steering_angle, resample=Image.BICUBIC, expand=False)
        img.paste(rotated_steer,
                  (int(col_positions[4] - icon_size / 2),
                   int(row_top - icon_size / 2)),
                  rotated_steer)

    accel_icon = load_icon('pedal_acc', icon_size)
    if accel_icon:
        draw_corner_circle(col_positions[4], row_pedal)
        base_accel = tint_icon(accel_icon, COLORS['dim_white'], grayscale=True)
        img.paste(base_accel,
                  (int(col_positions[4] - icon_size / 2),
                   int(row_pedal - icon_size / 2)),
                  base_accel)

        if accel_value > 0:
            colored_accel = tint_icon(accel_icon, COLORS['blue'])
            fill_height = int(icon_size * (accel_value / 100))
            if fill_height > 0:
                mask = Image.new('L', (icon_size, icon_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rectangle([0, icon_size - fill_height, icon_size, icon_size], fill=255)
                combined_mask = ImageChops.multiply(colored_accel.split()[3], mask)
                colored_accel.putalpha(combined_mask)
                img.paste(colored_accel,
                          (int(col_positions[4] - icon_size / 2),
                           int(row_pedal - icon_size / 2)),
                          colored_accel)

    # G力图 - 放在画面左下角，放大显示
    gforce_size = int(190 * scale)
    gforce_x = int(130 * scale)
    gforce_y = video_height - int(130 * scale)

    acc_x = sei['linear_acceleration_mps2_x']
    acc_y = sei['linear_acceleration_mps2_y']
    gx = acc_x / GRAVITY
    gy = acc_y / GRAVITY

    draw_gforce_meter(draw, gforce_x, gforce_y, gforce_size, gx, gy)

    # 垂直 G 力仪表 - 右下角（z 轴，侧视轿车 + 弹簧），与左下 G 力球边距对称
    acc_z = sei['linear_acceleration_mps2_z']
    gz = acc_z / GRAVITY
    vg_size = int(gforce_size * 0.95)  # 略小于 G 力球
    # G 力球边缘到画面边界的距离（球心 130*scale、半径 gforce_size//2 - 2）
    gf_margin = int(130 * scale) - (gforce_size // 2 - 2)
    draw_vertical_g_meter(img, draw, video_width, video_height, vg_size, gz, gf_margin,
                          brake_applied=sei['brake_applied'])

    # event.json 相关元素（没有 event.json 时自动跳过；触发原因已移至左上角信息条）
    if event:
        draw_minimap(img, draw, event, video_width, video_height, scale, font_map, font_map_small, frame_index)

    output_path = os.path.join(output_dir, f'overlay_{frame_index:06d}.png')
    img.save(output_path)


def generate_overlay_frames(sei_data, output_dir, base_time, video_width=1920, video_height=1080, use_metric=True, event=None, camera='front'):
    global gforce_history
    gforce_history = []
    _rainbow_flow.update(D=0.0, last_frame=-1)  # 重置彩虹流动累积器
    _blinker_state.update(left_prev=False, left_start=0, right_prev=False, right_start=0)
    os.makedirs(output_dir, exist_ok=True)

    for i, sei in enumerate(sei_data):
        generate_overlay_frame(sei, i, output_dir, base_time, video_width, video_height, use_metric, event, camera)

    return len(sei_data)


def get_video_info(video_path):
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_entries', 'format=duration', video_path],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    data = json.loads(result.stdout)
    duration_ms = float(data['format']['duration']) * 1000

    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_entries', 'stream=width,height', video_path],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    data = json.loads(result.stdout)
    streams = data.get('streams', [])
    if streams:
        width = int(streams[0]['width'])
        height = int(streams[0]['height'])
    else:
        width, height = 1920, 1080

    return duration_ms, width, height


def get_video_duration(video_path):
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_entries', 'format=duration', video_path],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    data = json.loads(result.stdout)
    return float(data['format']['duration']) * 1000


def process_video(input_video, input_csv, output_video, use_metric=True):
    print(f"Processing {input_video}...")

    # 临时目录按输出文件名唯一化，避免同一文件夹并行处理时互相冲突
    out_base = os.path.splitext(os.path.basename(output_video))[0]
    temp_dir = os.path.join(os.path.dirname(output_video), f'temp_frames_{out_base}')
    shutil.rmtree(temp_dir, ignore_errors=True)

    # 如果 CSV 不存在，自动从 MP4 提取 SEI 数据
    if not os.path.exists(input_csv):
        print(f"CSV not found, extracting SEI from video...")
        raw_sei = extract_sei_from_mp4(input_video)
        save_sei_csv(raw_sei, input_csv)
        print(f"Extracted {len(raw_sei)} SEI records to {input_csv}")
    # 统一从 CSV 读取，确保布尔字段正确解析
    sei_data = extract_sei_from_csv(input_csv)
    print(f"Loaded {len(sei_data)} SEI data points")

    # CSV 同步导出到输出视频旁边（已存在/被占用则跳过，不影响主流程）
    output_csv = os.path.splitext(output_video)[0] + '_sei.csv'
    if os.path.abspath(input_csv) != os.path.abspath(output_csv):
        try:
            if os.path.exists(output_csv):
                os.remove(output_csv)
            shutil.copy(input_csv, output_csv)
        except (PermissionError, OSError) as e:
            print(f"Warning: CSV 导出被占用已跳过 ({e})，不影响视频生成")

    # 读取 event.json（没有则返回 None，自动忽略）
    event = load_event_info(input_video)
    if event:
        print(f"Event: {event['city']} {event['street']} ({event['est_lat']}, {event['est_lon']}) - {event['reason_cn']}")

    video_duration, video_width, video_height = get_video_info(input_video)
    print(f"Video duration: {video_duration:.1f}ms")
    print(f"Video resolution: {video_width}x{video_height}")

    # 渲染前一次性取好地图，确保整段视频一致（避免开头一瞬显示伪路网）
    prefetch_map_image(event, video_width, video_height)

    base_time = parse_video_timestamp(input_video)
    print(f"Video recording time: {base_time}")

    camera = detect_camera_type(input_video)
    # 自动测量本段道路的真实虚线周期（结果缓存复用），用于把彩虹按「米」铺在路面上；
    # 全程未开启辅助驾驶时不会画彩虹，无需测量
    if any(s['autopilot_state'] in ('AUTOSTEER', 'SELF_DRIVING') for s in sei_data):
        RAINBOW_CALIB['period_m'], RAINBOW_CALIB['source'] = measure_dash_period(
            input_video, input_csv, camera)
    generate_overlay_frames(sei_data, temp_dir, base_time, video_width, video_height, use_metric, event, camera)
    print(f"Generated overlay frames in {temp_dir}")

    frame_rate = len(sei_data) / (video_duration / 1000)

    ffmpeg_cmd = [
        'ffmpeg', '-i', input_video,
        '-framerate', str(frame_rate), '-i', os.path.join(temp_dir, 'overlay_%06d.png'),
        '-filter_complex', "[0:v][1:v]overlay=0:0[vout]",
        '-map', '[vout]',
        '-c:a', 'copy',
        '-y',
        output_video
    ]

    print(f"Running FFmpeg command...")
    result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
    shutil.rmtree(temp_dir, ignore_errors=True)
    if result.returncode != 0:
        print(f"FFmpeg error: {result.stderr}")
        if os.path.exists(output_video):
            os.remove(output_video)
        return False

    print(f"Cleaned up temp files")
    print(f"Successfully created: {output_video}")
    return True


def process_folder(input_folder, output_folder, use_metric=True):
    os.makedirs(output_folder, exist_ok=True)

    # 直接按 mp4 扫描，没有 CSV 时 process_video 会自动提取
    video_files = [f for f in os.listdir(input_folder)
                   if f.lower().endswith('.mp4') and '_with_overlay' not in f]

    for video_file in video_files:
        camera_name = os.path.splitext(video_file)[0]
        video_path = os.path.join(input_folder, video_file)
        input_csv = os.path.join(input_folder, f"{camera_name}_sei.csv")
        output_video = os.path.join(output_folder, f"{camera_name}_with_overlay.mp4")

        process_video(video_path, input_csv, output_video, use_metric)


def process_csv_only(input_path):
    """只提取 SEI 数据生成 CSV，不渲染视频。支持单个视频文件或整个文件夹。"""
    if os.path.isfile(input_path):
        targets = [input_path]
    elif os.path.isdir(input_path):
        targets = [os.path.join(input_path, f)
                   for f in os.listdir(input_path)
                   if f.lower().endswith('.mp4') and '_with_overlay' not in f]
    else:
        print(f"Input not found: {input_path}")
        return

    for mp4 in targets:
        csv_path = os.path.splitext(mp4)[0] + '_sei.csv'
        sei_data = extract_sei_from_mp4(mp4)
        save_sei_csv(sei_data, csv_path)
        print(f"Extracted {len(sei_data)} frames -> {csv_path}")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Process Tesla dashcam videos with telemetry overlay')
    parser.add_argument('input', nargs='?', help='Input video file or folder')
    parser.add_argument('output', nargs='?',
                        help='Output video file or folder (not needed with --csv-only)')
    parser.add_argument('--metric', action='store_true', default=True, help='Use KM/H (default)')
    parser.add_argument('--imperial', action='store_true', help='Use MPH')
    parser.add_argument('--csv-only', action='store_true',
                        help='Only extract SEI data to CSV, skip video rendering')
    parser.add_argument('--clear-map-cache', action='store_true',
                        help='清空地图瓦片缓存后退出')

    args = parser.parse_args()

    use_metric = args.metric and not args.imperial

    if args.clear_map_cache:
        removed = clear_map_cache()
        print(f"地图缓存已清空：删除 {removed} 个文件")
    elif not args.input:
        parser.error('需要提供 input 视频/文件夹，或使用 --clear-map-cache')
    elif args.csv_only:
        process_csv_only(args.input)
    elif os.path.isfile(args.input):
        # 省略 output 时，自动在原视频旁生成 原名_with_overlay.mp4
        if args.output:
            output_video = args.output
        else:
            base, _ = os.path.splitext(args.input)
            output_video = base + '_with_overlay.mp4'
        input_csv = os.path.splitext(args.input)[0] + '_sei.csv'
        process_video(args.input, input_csv, output_video, use_metric)
    elif os.path.isdir(args.input):
        # 省略 output 时，成片默认生成回输入文件夹（与原视频同目录）
        output_folder = args.output if args.output else args.input
        process_folder(args.input, output_folder, use_metric)
    else:
        print(f"Input not found: {args.input}")