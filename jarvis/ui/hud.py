"""The floating panel (AppKit side): an animated orb, a status line and the live conversation.

What to draw comes from hud_logic.py; this module only renders it. All methods run on the main thread
(the app bridge takes care of that).
"""
from __future__ import annotations

import logging
import time

import AppKit
import objc
from AppKit import (
    NSAppearance,
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGradient,
    NSMutableParagraphStyle,
    NSPanel,
    NSParagraphStyleAttributeName,
    NSScreen,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSView,
    NSVisualEffectView,
)
from Foundation import NSMakePoint, NSMakeRect, NSMakeSize, NSObject, NSPointInRect, NSRunLoop, NSTimer
from PyObjCTools import AppHelper

from .hud_logic import (HEIGHT, ORB, PAD, STATE_COLORS, WIDTH, orb_shapes, panel_origin, smooth_level,
                        text_to_append, title_for)

log = logging.getLogger(__name__)

STYLE_NONACTIVATING = 1 << 7            # NSWindowStyleMaskNonactivatingPanel (borderless otherwise)
BACKING_BUFFERED = 2
STATUS_LEVEL = getattr(AppKit, "NSStatusWindowLevel", 25)
ALL_SPACES = 1 | 16 | 64 | 256          # join all spaces, stationary, ignores cycle, full-screen aux
COMMON_MODES = getattr(AppKit, "NSRunLoopCommonModes", "kCFRunLoopCommonModes")

def rgb(hex_color: str, alpha: float = 1.0):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


def weight(name: str, fallback: float) -> float:
    return float(getattr(AppKit, name, fallback))


def oval(cx: float, cy: float, r: float):
    return NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - r, cy - r, 2 * r, 2 * r))


def draw_orb(view) -> None:
    bounds = view.bounds()
    w, h = bounds.size.width, bounds.size.height
    cx, cy = w / 2.0, h / 2.0
    color = rgb(STATE_COLORS.get(view.jstate, "#6F8BA6"))
    for shape in orb_shapes(view.jstate, view.jphase, view.jsmooth, min(w, h)):
        kind = shape[0]
        if kind == "disc":
            _, radius, alpha = shape
            color.colorWithAlphaComponent_(alpha).set()
            oval(cx, cy, radius).fill()
        elif kind == "ring":
            _, radius, alpha, width = shape
            ring = oval(cx, cy, radius)
            ring.setLineWidth_(width)
            color.colorWithAlphaComponent_(alpha).set()
            ring.stroke()
        elif kind == "arc":
            _, radius, start, end, alpha, width = shape
            arc = NSBezierPath.bezierPath()
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(NSMakePoint(cx, cy), radius, start, end)
            arc.setLineWidth_(width)
            arc.setLineCapStyle_(1)  # round caps
            color.colorWithAlphaComponent_(alpha).set()
            arc.stroke()
        elif kind == "core":
            light = color.blendedColorWithFraction_ofColor_(0.55, NSColor.whiteColor()) or color
            dark = color.shadowWithLevel_(0.35) or color
            gradient = NSGradient.alloc().initWithStartingColor_endingColor_(light, dark)
            gradient.drawInBezierPath_relativeCenterPosition_(oval(cx, cy, shape[1]), NSMakePoint(-0.35, 0.35))


class JarvisOrbView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(JarvisOrbView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.jstate = "idle"
        self.jlevel = 0.0
        self.jsmooth = 0.0
        self.jphase = 0.0
        return self

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        try:
            draw_orb(self)
        except Exception:
            log.exception("Orb drawing failed")


class JarvisTicker(NSObject):
    """NSTimer target that calls a Python function."""

    def fire_(self, timer):
        callback = getattr(self, "jcallback", None)
        if callback is not None:
            try:
                callback()
            except Exception:
                log.exception("Animation tick failed")


def start_timer(ticker, interval: float):
    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(interval, ticker, "fire:", None, True)
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, COMMON_MODES)
    return timer


def make_label(frame, size: float, weight_value: float, color, lines: int = 1):
    field = NSTextField.alloc().initWithFrame_(frame)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setFont_(NSFont.systemFontOfSize_weight_(size, weight_value))
    field.setTextColor_(color)
    field.setStringValue_("")
    if lines > 1:
        field.setMaximumNumberOfLines_(lines)
        field.cell().setWraps_(True)
        field.cell().setTruncatesLastVisibleLine_(True)
        field.setLineBreakMode_(0)      # word wrap
    else:
        field.setLineBreakMode_(4)      # truncate tail
    return field


class Hud:
    def __init__(self, cfg, idle_hint: str = "") -> None:
        self.cfg = cfg
        self.idle_hint = idle_hint
        self.state = "idle"
        self.visible = False
        self.capture_hidden = False
        self.hide_token = 0
        self.last_kind = ""
        self.last_tick = time.monotonic()
        self.timer = None
        self.ticker = JarvisTicker.alloc().init()
        self.ticker.jcallback = self.tick
        self.styles = self._make_styles()
        self._build()

    # ------------------------------------------------------------ construction

    def _make_styles(self) -> dict:
        para = NSMutableParagraphStyle.alloc().init()
        para.setLineSpacing_(2.0)
        para.setParagraphSpacing_(4.0)
        regular = weight("NSFontWeightRegular", 0.0)
        medium = weight("NSFontWeightMedium", 0.23)
        return {
            "response": {NSFontAttributeName: NSFont.systemFontOfSize_weight_(13.0, regular),
                         NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(1.0, 0.92),
                         NSParagraphStyleAttributeName: para},
            "status": {NSFontAttributeName: NSFont.systemFontOfSize_weight_(11.5, medium),
                       NSForegroundColorAttributeName: rgb("#9DB4FF", 0.9),
                       NSParagraphStyleAttributeName: para},
            "error": {NSFontAttributeName: NSFont.systemFontOfSize_weight_(12.5, medium),
                      NSForegroundColorAttributeName: rgb("#FF7A6B"),
                      NSParagraphStyleAttributeName: para},
        }

    def _frame(self):
        screens = NSScreen.screens()
        screen = screens[0] if screens else NSScreen.mainScreen()
        vf = screen.visibleFrame()
        x, y = panel_origin((vf.origin.x, vf.origin.y, vf.size.width, vf.size.height),
                            str(getattr(self.cfg.ui, "hud_position", "top-right")))
        return NSMakeRect(x, y, WIDTH, HEIGHT)

    def _build(self) -> None:
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            self._frame(), STYLE_NONACTIVATING, BACKING_BUFFERED, False)
        panel.setLevel_(STATUS_LEVEL)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setCollectionBehavior_(ALL_SPACES)
        panel.setMovableByWindowBackground_(True)
        try:
            panel.setFloatingPanel_(True)
            panel.setBecomesKeyOnlyIfNeeded_(True)
            dark = getattr(AppKit, "NSAppearanceNameDarkAqua", "NSAppearanceNameDarkAqua")
            panel.setAppearance_(NSAppearance.appearanceNamed_(dark))
        except Exception:
            pass

        effect = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        effect.setMaterial_(13)        # HUD window material
        effect.setBlendingMode_(0)     # behind window
        effect.setState_(1)            # always active
        effect.setWantsLayer_(True)
        try:
            effect.layer().setCornerRadius_(18.0)
            effect.layer().setMasksToBounds_(True)
        except Exception:
            pass
        panel.setContentView_(effect)

        self.orb = JarvisOrbView.alloc().initWithFrame_(NSMakeRect(PAD, HEIGHT - PAD - ORB, ORB, ORB))
        effect.addSubview_(self.orb)

        text_x = PAD + ORB + 14.0
        text_w = WIDTH - text_x - PAD
        self.title = make_label(NSMakeRect(text_x, HEIGHT - PAD - 22.0, text_w, 22.0), 15.0,
                                weight("NSFontWeightSemibold", 0.3), NSColor.colorWithWhite_alpha_(1.0, 0.95))
        self.subtitle = make_label(NSMakeRect(text_x, HEIGHT - PAD - ORB - 6.0, text_w, 32.0), 12.0,
                                   weight("NSFontWeightRegular", 0.0), NSColor.colorWithWhite_alpha_(1.0, 0.6), lines=2)
        effect.addSubview_(self.title)
        effect.addSubview_(self.subtitle)

        body_h = HEIGHT - PAD * 2 - ORB - 14.0
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, PAD, WIDTH - 2 * PAD, body_h))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(0)
        content_w = scroll.contentSize().width
        text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, content_w, body_h))
        text.setEditable_(False)
        text.setSelectable_(False)
        text.setDrawsBackground_(False)
        text.setRichText_(True)
        text.setVerticallyResizable_(True)
        text.setHorizontallyResizable_(False)
        text.setAutoresizingMask_(2)    # width sizable
        text.setTextContainerInset_(NSMakeSize(0.0, 2.0))
        text.setMinSize_(NSMakeSize(0.0, body_h))
        text.setMaxSize_(NSMakeSize(1.0e7, 1.0e7))
        text.textContainer().setContainerSize_(NSMakeSize(content_w, 1.0e7))
        text.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(text)
        effect.addSubview_(scroll)

        self.panel = panel
        self.textview = text
        self.set_state("idle")

    # ------------------------------------------------------------ visibility

    def show(self) -> None:
        self.hide_token += 1
        if not self.visible:
            self.panel.setFrame_display_(self._frame(), False)
            self.panel.setAlphaValue_(0.0 if self.capture_hidden else 1.0)
            self.panel.orderFrontRegardless()
            self.visible = True
            self.last_tick = time.monotonic()
            if self.timer is None:
                self.timer = start_timer(self.ticker, 1.0 / 30.0)

    def hide(self) -> None:
        self.hide_token += 1
        if self.visible:
            self.panel.orderOut_(None)
            self.visible = False
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None

    def schedule_hide(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.hide_token += 1
        AppHelper.callLater(seconds, self._autohide, self.hide_token)

    def _autohide(self, token: int) -> None:
        if token != self.hide_token or not self.visible:
            return
        if NSPointInRect(NSEvent.mouseLocation(), self.panel.frame()):
            self.schedule_hide(3.0)  # the user is reading it
            return
        if self.state in ("idle", "error"):
            self.hide()

    def set_capture_hidden(self, hidden: bool) -> None:
        self.capture_hidden = hidden
        if self.visible:
            self.panel.setAlphaValue_(0.0 if hidden else 1.0)

    # ------------------------------------------------------------ content

    def set_state(self, state: str, title: str | None = None, subtitle: str | None = None) -> None:
        self.state = state
        self.orb.jstate = state
        if state != "listening":
            self.orb.jlevel = 0.0
        self.title.setStringValue_(title_for(state, title))
        if subtitle is not None:
            self.subtitle.setStringValue_(subtitle)
        elif state == "idle":
            self.subtitle.setStringValue_(self.idle_hint)
        self.orb.setNeedsDisplay_(True)
        if state not in ("idle",):
            self.show()

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setStringValue_(text or "")

    def set_level(self, level: float) -> None:
        self.orb.jlevel = max(0.0, min(1.0, float(level)))

    def clear_text(self) -> None:
        self.textview.setString_("")
        self.last_kind = ""

    def append(self, text: str, kind: str = "response") -> None:
        if not text:
            return
        storage = self.textview.textStorage()
        text = text_to_append(str(storage.string()), text, kind, self.last_kind)
        attributed = NSAttributedString.alloc().initWithString_attributes_(text, self.styles.get(kind, self.styles["response"]))
        storage.appendAttributedString_(attributed)
        self.last_kind = kind
        self.textview.scrollRangeToVisible_((storage.length(), 0))
        self.show()

    def text_is_empty(self) -> bool:
        return self.textview.textStorage().length() == 0

    # ------------------------------------------------------------ animation

    def tick(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self.last_tick)
        self.last_tick = now
        orb = self.orb
        orb.jphase = orb.jphase + dt
        orb.jsmooth = smooth_level(orb.jsmooth, orb.jlevel, dt)
        orb.setNeedsDisplay_(True)
