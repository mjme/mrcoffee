"""The menu bar app: a thin AppKit layer that renders what the reconciler reports."""

import logging
from importlib.resources import files

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSStatusBar,
    NSTimer,
    NSVariableStatusItemLength,
)

from . import db
from .reconciler import Reconciler
from .runner import SubprocessRunner, install_exit_handlers, single_instance_lock

log = logging.getLogger("mrcoffee")
TICK_SECONDS = 2.0


def bundled_icon(name: str):
    image = NSImage.alloc().initWithContentsOfFile_(str(files("mrcoffee") / "icons" / f"{name}.png"))
    image.setSize_((18, 18))
    return image


def icon(awake: bool):
    name = "cup.and.saucer.fill" if awake else "cup.and.saucer"
    # SF Symbols need macOS 11; before that, fall back to the bundled PNG.
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "Mr. Coffee") or bundled_icon(name)
    image.setTemplate_(True)
    return image


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        def add(title, action=None):
            item = menu.addItemWithTitle_action_keyEquivalent_(title, action, "")
            item.setTarget_(self)
            item.setEnabled_(action is not None)
            return item

        self.status = add("Off")
        menu.addItem_(NSMenuItem.separatorItem())
        self.manual = add("Keep Awake", "toggleManual:")
        self.display = add("Keep Display On", "toggleDisplay:")
        self.release = add("Release Agent Holds", "releaseAgents:")
        menu.addItem_(NSMenuItem.separatorItem())
        add("Quit", "quit:")
        self.item.setMenu_(menu)

        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(TICK_SECONDS, self, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)  # also fires while the menu is open
        self.tick_(None)

    @objc.python_method
    def render(self, view):
        self.item.button().setImage_(icon(view.awake))
        self.status.setTitle_(view.status)
        self.manual.setState_(int(view.manual))
        self.display.setState_(int(view.keep_display))
        self.release.setEnabled_(view.agents > 0)

    def tick_(self, sender):
        self.render(self.reconciler.tick())

    def toggleManual_(self, sender):
        self.render(self.reconciler.act(db.toggle_manual))

    def toggleDisplay_(self, sender):
        self.render(self.reconciler.act(db.toggle_keep_display))

    def releaseAgents_(self, sender):
        self.render(self.reconciler.act(db.release_agents))

    def quit_(self, sender):
        log.info("quit chosen from the menu")
        self.reconciler.shutdown()  # explicitly: terminate_ calls C exit(), which skips Python's atexit
        NSApplication.sharedApplication().terminate_(None)  # exits with status 0


def main() -> int:
    db.home().mkdir(parents=True, exist_ok=True)
    lock = single_instance_lock(db.home() / "app.lock")  # held until the process exits
    if lock is None:
        log.info("another instance is already running; exiting")
        return 0
    log.info("app starting")
    reconciler = Reconciler(SubprocessRunner())
    reconciler.sweep()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon
    delegate = AppDelegate.alloc().init()
    delegate.reconciler = reconciler
    app.setDelegate_(delegate)
    install_exit_handlers(reconciler.shutdown, exit=lambda: app.terminate_(None))
    app.run()
    return 0
