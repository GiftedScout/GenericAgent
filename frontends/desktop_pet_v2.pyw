"""Desktop Pet with Skin System — Cross-platform with True Transparency"""
import os, re, sys, json, threading, io
from PIL import Image, ImageDraw, ImageFont, ImageOps

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
SKINS_DIR = os.path.join(SCRIPT_DIR, 'skins')

PET_LOCK_PORT = 19533               # same convention as the other frontends (QQ 19528, DingTalk 19530, Discord 19532)
_LOCK_SOCK = None                   # kept alive for the whole process: the kernel frees the port on exit

class SkinLoader:
    """Load and parse skin configuration"""
    @staticmethod
    def load_skin(skin_path):
        """Load skin.json and return skin config"""
        config_file = os.path.join(skin_path, 'skin.json')
        if not os.path.exists(config_file):
            raise FileNotFoundError(f"skin.json not found in {skin_path}")

        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        if 'animations' not in config:
            raise ValueError("skin.json must contain 'animations' field")

        config['path'] = skin_path
        return config

    @staticmethod
    def list_skins():
        """List all available skins"""
        if not os.path.exists(SKINS_DIR):
            return []

        skins = []
        for item in os.listdir(SKINS_DIR):
            skin_path = os.path.join(SKINS_DIR, item)
            if os.path.isdir(skin_path):
                config_file = os.path.join(skin_path, 'skin.json')
                if os.path.exists(config_file):
                    skins.append(item)
        return skins

class AnimationLoader:
    """Load animation frames from sprite sheet"""
    @staticmethod
    def load_sprite_frames(skin_path, anim_config):
        """Load frames from sprite sheet"""
        file_path = os.path.join(skin_path, anim_config['file'])
        sprite_config = anim_config['sprite']

        img = Image.open(file_path)
        frames = []

        frame_width = sprite_config['frameWidth']
        frame_height = sprite_config['frameHeight']
        frame_count = sprite_config['frameCount']
        columns = sprite_config['columns']
        start_frame = sprite_config.get('startFrame', 0)

        for i in range(frame_count):
            frame_idx = start_frame + i
            row = frame_idx // columns
            col = frame_idx % columns

            x = col * frame_width
            y = row * frame_height

            frame = img.crop((x, y, x + frame_width, y + frame_height))
            frames.append(frame)

        return frames


def _load_default_font(size):
    """Load a usable font for bubble text."""
    font_candidates = [
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/System/Library/Fonts/PingFang.ttc',
        '/System/Library/Fonts/STHeiti Light.ttc',
        'C:/Windows/Fonts/msyh.ttc',
        'C:/Windows/Fonts/simhei.ttf',
        'C:/Windows/Fonts/arial.ttf',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
        '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
    ]
    for font_path in font_candidates:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def _normalize_bubble_text(text):
    """Normalize text for fonts that cannot render some symbols."""
    text = (text or '').strip()
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if lines:
        turn_match = re.match(r'^\s*🔄?\s*Turn\s+(\d+)\s*$', lines[0], flags=re.IGNORECASE)
        if turn_match:
            rest = '\n'.join(line.strip() for line in lines[1:] if line.strip())
            return f"Turn {turn_match.group(1)}: {rest}" if rest else f"Turn {turn_match.group(1)}:"
    return text.replace('🔄 Turn', 'Turn').replace('🔄', '').strip()


def _wrap_text_for_width(draw, text, font, max_width):
    """Wrap text to fit inside max_width."""
    text = _normalize_bubble_text(text)
    if not text:
        return ['']

    paragraphs = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    lines = []

    for paragraph in paragraphs:
        if not paragraph:
            lines.append('')
            continue

        current = ''
        for ch in paragraph:
            candidate = current + ch
            bbox = draw.textbbox((0, 0), candidate, font=font)
            width = bbox[2] - bbox[0]
            if current and width > max_width:
                lines.append(current)
                current = ch
            else:
                current = candidate
        if current:
            lines.append(current)

    return lines or ['']


def build_bubble_image(message, max_width=220):
    """Build a PIL image for the toast bubble using the user asset when available."""
    message = (message or '').strip()
    bubble_path = next((p for p in [os.path.join(SCRIPT_DIR, 'chat_bubble.png'),
                                     os.path.join(SCRIPT_DIR, 'bubble.png')]
                        if os.path.exists(p)), None)

    if bubble_path:
        bubble = Image.open(bubble_path).convert('RGBA')
    else:
        bubble = Image.new('RGBA', (256, 128), (255, 255, 255, 0))
        draw = ImageDraw.Draw(bubble)
        draw.rounded_rectangle((8, 8, 247, 87), radius=12, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=3)
        draw.polygon([(48, 87), (72, 87), (56, 112)], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))

    bubble = ImageOps.contain(bubble, (max_width, max(64, int(max_width * bubble.height / bubble.width))), Image.NEAREST)

    # Detect the actual opaque bubble region to position text correctly
    alpha = bubble.getchannel('A')
    content_box = alpha.getbbox()  # (left, top, right, bottom) of opaque area
    if content_box:
        cb_left, cb_top, cb_right, cb_bottom = content_box
    else:
        cb_left, cb_top, cb_right, cb_bottom = 0, 0, bubble.width, bubble.height
    content_w = cb_right - cb_left
    content_h = cb_bottom - cb_top

    font_size = max(12, content_h // 6)
    font = _load_default_font(font_size)
    draw = ImageDraw.Draw(bubble)

    # Padding relative to the opaque bubble region, not the full image
    inner_pad_x = max(6, content_w // 14)
    inner_pad_top = max(4, content_h // 12)
    inner_pad_bottom = max(12, content_h // 4)
    text_area_width = max(36, content_w - inner_pad_x * 2)

    lines = _wrap_text_for_width(draw, message, font, text_area_width)
    ascent, descent = font.getmetrics() if hasattr(font, 'getmetrics') else (font_size, font_size // 4)
    line_height = max(font_size, ascent + descent)
    usable_h = content_h - inner_pad_top - inner_pad_bottom
    max_lines = max(1, usable_h // line_height)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if lines:
            last = lines[-1]
            while last and draw.textbbox((0, 0), last + '…', font=font)[2] > text_area_width:
                last = last[:-1]
            lines[-1] = (last + '…') if last else '…'

    total_text_height = len(lines) * line_height
    y = cb_top + inner_pad_top + max(0, (usable_h - total_text_height) // 2) - 3

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = cb_left + inner_pad_x + (text_area_width - text_width) / 2
        draw.text((x, y), line, font=font, fill=(32, 32, 32, 255))
        y += line_height

    alpha = bubble.getchannel('A')
    bbox = alpha.getbbox()
    if bbox:
        bubble = bubble.crop(bbox)

    width, height = bubble.size
    alpha = bubble.getchannel('A')
    bottom_y = height - 1
    tail_x = width // 2
    for y in range(height - 1, -1, -1):
        xs = [x for x in range(width) if alpha.getpixel((x, y)) > 0]
        if xs:
            bottom_y = y
            tail_x = xs[len(xs) // 2]
            break

    return {
        'image': bubble,
        'size': bubble.size,
        'tail_tip': (tail_x, bottom_y),
    }

# ============================================================================
# Shared Base Class
# ============================================================================
class PetBase:
    """Shared logic for Mac and Windows pet implementations."""

    def _schedule_main(self, fn):
        """Schedule fn on the GUI main thread. Subclasses must override."""
        raise NotImplementedError

    def set_state_safe(self, state):
        """Thread-safe wrapper for set_state."""
        self._schedule_main(lambda: self.set_state(state))

    def show_toast_safe(self, message):
        """Thread-safe wrapper for show_toast."""
        self._schedule_main(lambda m=message: self.show_toast(m))

    def _connect_hub(self):
        """Join the local hub under the fixed name 'pet': no server, no port, one bus peer."""
        sys.path.insert(0, SCRIPT_DIR); import hub
        hub.serve()                                  # best effort: bring the hub up if nobody listens yet
        hub.HubClient('pet', fixed=True, sub=['turn'], on_ev=self._on_ev).start()

    def _on_ev(self, c):
        """Bus push: {'state':..} drives the animation, {'msg':..} pops a toast."""
        d = c.get('data') or {}
        if d.get('state'): self.set_state_safe(d['state'])
        if d.get('msg'): self.show_toast_safe(d['msg'])


# ============================================================================
# macOS Implementation - Pure Cocoa with True Transparency
# ============================================================================
from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QAction, QCursor, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QWidget

class _LinuxPetLabel(QLabel):
    def __init__(self, pet):
        super().__init__()
        self.pet = pet
        self.drag_offset = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.pet.window.frameGeometry().topLeft()
            event.accept()
            return
        if event.button() == Qt.RightButton:
            self.pet._show_context_menu(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None and (event.buttons() & Qt.LeftButton):
            self.pet.window.move(event.globalPosition().toPoint() - self.drag_offset)
            self.pet._reposition_toast()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            QApplication.instance().quit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class LinuxPet(PetBase):
    def __init__(self, skin_name=None):
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.available_skins = SkinLoader.list_skins()
        self.load_skin(skin_name)

        screen = self.app.primaryScreen()
        screen_geo = screen.availableGeometry() if screen else None
        if screen_geo:
            x_pos = screen_geo.right() - self.display_width - 72
            y_pos = screen_geo.bottom() - self.display_height - 120
        else:
            x_pos, y_pos = 1200, 700

        self.window = QWidget()
        self.window.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.window.setAttribute(Qt.WA_TranslucentBackground, True)
        self.window.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.window.resize(self.display_width, self.display_height)
        self.window.move(x_pos, y_pos)

        self.label = _LinuxPetLabel(self)
        self.label.setParent(self.window)
        self.label.setGeometry(0, 0, self.display_width, self.display_height)
        self.label.setAttribute(Qt.WA_TranslucentBackground, True)
        self.label.setStyleSheet('background: transparent;')
        self.label.setScaledContents(True)

        self.current_state = 'idle'
        self.frame_idx = 0
        self.toast_window = None
        self.toast_label = None
        self.toast_pixmap = None

        self.anim_timer = QTimer()
        self.anim_timer.timeout.connect(self._animate)
        self._restart_animation_timer()

        self.window.show()
        self._connect_hub()

        print(f"✓ Linux PySide6 Pet started at ({x_pos}, {y_pos})")
        print(f"  Animations: {', '.join(self.animations.keys())}")

    def _pil_to_qpixmap(self, pil_img):
        buffer = io.BytesIO()
        pil_img.save(buffer, format='PNG')
        qimage = QImage.fromData(buffer.getvalue(), 'PNG')
        return QPixmap.fromImage(qimage)

    def load_skin(self, skin_name=None):
        available_skins = SkinLoader.list_skins()
        if not available_skins:
            raise FileNotFoundError(f"No skins found in {SKINS_DIR}")

        if skin_name is None or skin_name not in available_skins:
            skin_name = available_skins[0]

        skin_path = os.path.join(SKINS_DIR, skin_name)
        self.skin_config = SkinLoader.load_skin(skin_path)

        display_size = self.skin_config.get('size', {})
        self.display_width = display_size.get('width', 128)
        self.display_height = display_size.get('height', 128)

        self.animations = {}
        for anim_name, anim_config in self.skin_config['animations'].items():
            pil_frames = AnimationLoader.load_sprite_frames(skin_path, anim_config)
            qt_frames = []
            for frame in pil_frames:
                if frame.mode != 'RGBA':
                    frame = frame.convert('RGBA')
                scaled = frame.resize((self.display_width, self.display_height), Image.NEAREST)
                qt_frames.append(self._pil_to_qpixmap(scaled))

            self.animations[anim_name] = {
                'frames': qt_frames,
                'fps': anim_config.get('sprite', {}).get('fps', 6)
            }

        if hasattr(self, 'window'):
            self.window.resize(self.display_width, self.display_height)
            self.label.setGeometry(0, 0, self.display_width, self.display_height)
            self._animate(force=True)
            self._reposition_toast()

    def _restart_animation_timer(self):
        anim = self.animations.get(self.current_state) or next(iter(self.animations.values()))
        fps = max(1, anim.get('fps', 6))
        self.anim_timer.start(int(1000 / fps))

    def _animate(self, force=False):
        if self.current_state not in self.animations:
            return
        anim = self.animations[self.current_state]
        frames = anim['frames']
        if not frames:
            return
        if force:
            self.frame_idx = 0
        self.label.setPixmap(frames[self.frame_idx])
        self.frame_idx = (self.frame_idx + 1) % len(frames)

    def set_state(self, state):
        if state in self.animations and state != self.current_state:
            self.current_state = state
            self.frame_idx = 0
            self._restart_animation_timer()
            print(f"→ State: {state}")

    def _show_context_menu(self, global_pos):
        menu = QMenu(self.window)
        for skin_name in SkinLoader.list_skins():
            action = QAction(skin_name, menu)
            action.triggered.connect(lambda checked=False, name=skin_name: self._change_skin(name))
            menu.addAction(action)
        menu.addSeparator()
        quit_action = QAction('Quit', menu)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_action)
        menu.popup(global_pos)

    def _compute_toast_geometry(self, bubble_width, bubble_height, tail_x, tail_y):
        pet_pos = self.window.frameGeometry().topLeft()
        anchor_x = pet_pos.x() + int(self.display_width * 0.75)
        anchor_y = pet_pos.y() + int(self.display_height * 0.15)
        return anchor_x - tail_x, anchor_y - tail_y - bubble_height // 2

    def show_toast(self, message):
        if self.toast_window:
            self.toast_window.close()
            self.toast_window = None
            self.toast_label = None
            self.toast_pixmap = None

        bubble_info = build_bubble_image(message, max_width=max(180, min(260, self.display_width * 2)))
        bubble_pil = bubble_info['image']
        bubble_width, bubble_height = bubble_info['size']
        tail_x, tail_y = bubble_info['tail_tip']
        self.toast_pixmap = self._pil_to_qpixmap(bubble_pil)

        self.toast_window = QWidget()
        self.toast_window.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.WindowTransparentForInput
        )
        self.toast_window.setAttribute(Qt.WA_TranslucentBackground, True)
        self.toast_window.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.toast_window.resize(bubble_width, bubble_height)

        self.toast_label = QLabel(self.toast_window)
        self.toast_label.setGeometry(0, 0, bubble_width, bubble_height)
        self.toast_label.setPixmap(self.toast_pixmap)
        self.toast_label.setAttribute(Qt.WA_TranslucentBackground, True)
        self.toast_label.setStyleSheet('background: transparent;')

        toast_x, toast_y = self._compute_toast_geometry(bubble_width, bubble_height, tail_x, tail_y)
        self.toast_window.move(toast_x, toast_y)
        self.toast_window.show()

        QTimer.singleShot(3000, self._hide_toast)
        print(f"Toast: {message}")

    def _reposition_toast(self):
        if not self.toast_window:
            return
        label_pixmap = self.toast_label.pixmap() if self.toast_label else None
        if label_pixmap is None:
            return
        bubble_width = label_pixmap.width()
        bubble_height = label_pixmap.height()
        toast_x, toast_y = self._compute_toast_geometry(
            bubble_width,
            bubble_height,
            bubble_width // 2,
            bubble_height
        )
        self.toast_window.move(toast_x, toast_y)

    def _hide_toast(self):
        if self.toast_window:
            self.toast_window.close()
            self.toast_window = None
            self.toast_label = None
            self.toast_pixmap = None

    def _schedule_main(self, fn):
        QTimer.singleShot(0, fn)

    def _change_skin(self, skin_name):
        print(f"Changing skin to: {skin_name}")
        self.load_skin(skin_name)
        self.current_state = 'idle'
        self.frame_idx = 0
        self._restart_animation_timer()

    def run(self):
        self.app.exec()

if __name__ == '__main__':
    import socket
    _LOCK_SOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _LOCK_SOCK.bind(("127.0.0.1", PET_LOCK_PORT))   # first one wins; a later launch just exits
    except OSError:
        sys.exit(0)
    pet = LinuxPet('vita')
    pet.run()

