"""The tutor pointer (AppKit side): a click-through, full-screen overlay with a glowing arrow and a
label bubble. Shapes, placement and motion come from overlay_logic.py; this module only draws them.
Coordinates are points from the top-left of the main display (the view is flipped), which is what
mac.Shot.to_screen() returns. Main-thread only.
"""
from __future__ import annotations

import logging
import math
import time

import AppKit
import objc
from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSRectFillUsingOperation,
    NSScreen,
    NSShadow,
    NSView,
    NSWindow,
)
from Foundation import NSMakePoint, NSMakeRect, NSMakeSize
from PyObjCTools import AppHelper

from .hud import JarvisTicker, rgb, start_timer, weight
from .overlay_logic import arrow_points, bubble_layout, from_mouse, glide, glow_rings, pointer_area, union

log = logging.getLogger(__name__)

OVERLAY_LEVEL = getattr(AppKit, "NSScreenSaverWindowLevel", 1000)
ALL_SPACES = 1 | 16 | 64 | 256
COMPOSITE_COPY = 1
AMBER = "#FFB547"
BUBBLE_BG = "#14181F"
TEXT_OPTIONS = 1 | 2  # uses line fragment origin | uses font leading


def rect(r):
    return NSMakeRect(r[0], r[1], r[2], r[3])


def label_layout(view):
    """(attributed text, text rect, bubble rect) for the current label, or None."""
    if not view.jlabel or view.jpos is None:
        return None
    x, y = view.jpos
    attrs = {NSFontAttributeName: NSFont.systemFontOfSize_weight_(15.0, weight("NSFontWeightMedium", 0.23)),
             NSForegroundColorAttributeName: NSColor.whiteColor()}
    text = NSAttributedString.alloc().initWithString_attributes_(view.jlabel, attrs)
    size = text.boundingRectWithSize_options_(NSMakeSize(300.0, 400.0), TEXT_OPTIONS).size
    bounds = view.bounds()
    bubble, text_rect = bubble_layout(x, y, math.ceil(size.width), math.ceil(size.height),
                                      bounds.size.width, bounds.size.height)
    return text, text_rect, bubble


def pointer_bounds(view):
    x, y = view.jpos
    layout = label_layout(view)
    return pointer_area(x, y, layout[2] if layout is not None else None)


def draw_pointer(view, dirty) -> None:
    NSColor.clearColor().set()
    NSRectFillUsingOperation(dirty, COMPOSITE_COPY)
    if view.jpos is None:
        return
    x, y = view.jpos
    amber = rgb(AMBER)
    for radius, alpha in glow_rings(view.jpulse):
        amber.colorWithAlphaComponent_(alpha).set()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - radius, y - radius, 2 * radius, 2 * radius)).fill()

    points = arrow_points(x, y)
    arrow = NSBezierPath.bezierPath()
    arrow.moveToPoint_(NSMakePoint(*points[0]))
    for point in points[1:]:
        arrow.lineToPoint_(NSMakePoint(*point))
    arrow.closePath()
    arrow.setLineJoinStyle_(1)
    NSGraphicsContext.saveGraphicsState()
    shadow = NSShadow.alloc().init()
    shadow.setShadowColor_(NSColor.colorWithWhite_alpha_(0.0, 0.45))
    shadow.setShadowBlurRadius_(6.0)
    shadow.setShadowOffset_(NSMakeSize(0.0, -2.0))
    shadow.set()
    amber.set()
    arrow.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSColor.whiteColor().set()
    arrow.setLineWidth_(1.6)
    arrow.stroke()

    layout = label_layout(view)
    if layout is None:
        return
    text, text_rect, bubble_rect = layout
    bubble = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect(bubble_rect), 10.0, 10.0)
    NSGraphicsContext.saveGraphicsState()
    shadow = NSShadow.alloc().init()
    shadow.setShadowColor_(NSColor.colorWithWhite_alpha_(0.0, 0.35))
    shadow.setShadowBlurRadius_(10.0)
    shadow.setShadowOffset_(NSMakeSize(0.0, -3.0))
    shadow.set()
    rgb(BUBBLE_BG, 0.92).set()
    bubble.fill()
    NSGraphicsContext.restoreGraphicsState()
    amber.colorWithAlphaComponent_(0.9).set()
    bubble.setLineWidth_(1.5)
    bubble.stroke()
    text.drawWithRect_options_(rect(text_rect), TEXT_OPTIONS)


class JarvisTutorView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(JarvisTutorView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.jpos = None
        self.jlabel = ""
        self.jpulse = 0.0
        self.jbounds = None
        return self

    def isFlipped(self):
        return True

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        try:
            draw_pointer(self, rect)
        except Exception:
            log.exception("Pointer drawing failed")


class TutorOverlay:
    def __init__(self) -> None:
        screen = NSScreen.screens()[0] if NSScreen.screens() else NSScreen.mainScreen()
        frame = screen.frame()
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(frame, 0, 2, False)
        window.setOpaque_(False)
        window.setBackgroundColor_(NSColor.clearColor())
        window.setIgnoresMouseEvents_(True)
        window.setLevel_(OVERLAY_LEVEL)
        window.setHasShadow_(False)
        window.setReleasedWhenClosed_(False)
        window.setCollectionBehavior_(ALL_SPACES)
        view = JarvisTutorView.alloc().initWithFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
        window.setContentView_(view)
        self.window = window
        self.view = view
        self.screen_height = frame.size.height
        self.visible = False
        self.capture_hidden = False
        self.hide_token = 0
        self.anim_from = None
        self.anim_to = None
        self.anim_start = 0.0
        self.last_tick = time.monotonic()
        self.timer = None
        self.ticker = JarvisTicker.alloc().init()
        self.ticker.jcallback = self.tick

    def point_at(self, x: float, y: float, label: str) -> None:
        self.hide_token += 1
        if self.view.jpos is None:  # glide in from wherever the mouse is
            mouse = NSEvent.mouseLocation()
            start = from_mouse(mouse.x, mouse.y, self.screen_height)
        else:
            start = self.view.jpos
        self.anim_from = start
        self.anim_to = (float(x), float(y))
        self.anim_start = time.monotonic()
        self.view.jlabel = label or ""
        self.view.jpos = start
        if not self.visible:
            self.window.setAlphaValue_(0.0 if self.capture_hidden else 1.0)
            self.window.orderFrontRegardless()
            self.visible = True
        if self.timer is None:
            self.last_tick = time.monotonic()
            self.timer = start_timer(self.ticker, 1.0 / 60.0)
        self._invalidate()

    def hide(self) -> None:
        self.hide_token += 1
        if self.view.jbounds is not None:
            self.view.setNeedsDisplayInRect_(rect(self.view.jbounds))
        self.view.jpos = None
        self.view.jlabel = ""
        self.view.jbounds = None
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        if self.visible:
            self.window.orderOut_(None)
            self.visible = False

    def hide_after(self, seconds: float) -> None:
        if not self.visible or seconds <= 0:
            return
        self.hide_token += 1
        token = self.hide_token
        AppHelper.callLater(seconds, self._hide_if, token)

    def _hide_if(self, token: int) -> None:
        if token == self.hide_token:
            self.hide()

    def set_capture_hidden(self, hidden: bool) -> None:
        self.capture_hidden = hidden
        if self.visible:
            self.window.setAlphaValue_(0.0 if hidden else 1.0)

    def _invalidate(self) -> None:
        view = self.view
        if view.jpos is None:
            return
        new = pointer_bounds(view)
        dirty = new if view.jbounds is None else union(view.jbounds, new)
        view.jbounds = new
        view.setNeedsDisplayInRect_(rect(dirty))

    def tick(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self.last_tick)
        self.last_tick = now
        view = self.view
        if view.jpos is None:
            return
        view.jpulse = view.jpulse + dt
        if self.anim_to is not None:
            view.jpos, arrived = glide(self.anim_from, self.anim_to, now - self.anim_start)
            if arrived:
                self.anim_to = None
        self._invalidate()
