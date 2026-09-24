import json
import objc
import threading
from collections import deque
from AppKit import (
    NSApp,
    NSAlert,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSMakeRect,
    NSOpenPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSSwitchButton,
    NSTableColumn,
    NSTableView,
    NSTextField,
    NSTextView,
    NSView,
    NSTitledWindowMask,
    NSClosableWindowMask,
    NSMiniaturizableWindowMask,
    NSResizableWindowMask,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialHUDWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindow,
    NSScrollView,
    NSViewWidthSizable,
    NSViewHeightSizable,
)
from Foundation import NSDefaultRunLoopMode, NSObject, NSURL

from core.controller import ControllerError, ProxyLoaderController
from core.health_check import STATUS_CHECKING, STATUS_DOWN, STATUS_UP
from core.proxy_entry import count_candidate_lines
from core.manager import ProxyManagerError
from . import app_bundle, system_proxy

WINDOW_WIDTH = 460
WINDOW_HEIGHT = 800
NSALERT_FIRST_BUTTON_RETURN = 1000
NSALERT_SECOND_BUTTON_RETURN = 1001
NSALERT_THIRD_BUTTON_RETURN = 1002
MAIN_THREAD_TIMEOUT = 30.0


class ProxyTableDataSource(NSObject):
    def initWithManager_(self, manager):
        self = objc.super(ProxyTableDataSource, self).init()
        if self is None:
            return None
        self.manager = manager
        return self

    def numberOfRowsInTableView_(self, table_view):
        return len(self.manager.entries)

    def tableView_objectValueForTableColumn_row_(self, table_view, column, row):
        entries = self.manager.entries
        if row >= len(entries):
            return ""
        if column.identifier() == "health":
            status = self.manager.health.get(row)
            dot = "○"
            if status is not None:
                if status.status == STATUS_UP:
                    dot = "●"
                elif status.status == STATUS_DOWN:
                    dot = "●"
                elif status.status == STATUS_CHECKING:
                    dot = "◐"
            label = status.label() if status else "—"
            return f"{dot} {label}"
        chain = self.manager.chain
        if chain and row in chain:
            marker = f"✓{chain.index(row) + 1}"
        elif not chain and row == self.manager.active_index:
            marker = "✓"
        else:
            marker = " "
        return f"{marker} {row}: {entries[row].label()}"


class AppTableDataSource(NSObject):
    def initWithTargets_manager_(self, targets, manager):
        self = objc.super(AppTableDataSource, self).init()
        if self is None:
            return None
        self.targets = targets
        self.manager = manager
        return self

    def numberOfRowsInTableView_(self, table_view):
        return len(self.targets)

    def tableView_objectValueForTableColumn_row_(self, table_view, column, row):
        if row >= len(self.targets):
            return ""
        target = self.targets[row]
        entries = self.manager.entries
        if target.proxy_index is not None and 0 <= target.proxy_index < len(entries):
            return f"{target.display_name}  →  {entries[target.proxy_index].label()}"
        return f"{target.display_name}  —  no proxy pinned (uses active)"


class GlassWindowController(NSObject):
    def initWithManager_(self, manager):
        self = objc.super(GlassWindowController, self).init()
        if self is None:
            return None

        self.manager = manager
        self.core = ProxyLoaderController(manager=manager)
        self._main_thread_tasks = deque()
        self._main_thread_tasks_lock = threading.Lock()
        self.mcp_host = None
        self._mcp_token = None

        self.data_source = ProxyTableDataSource.alloc().initWithManager_(manager)
        self.apps_data_source = AppTableDataSource.alloc().initWithTargets_manager_(
            self.core.app_targets, manager
        )
        self._build_window()
        self._apply_mode_to_ui()
        self.table.reloadData()
        self.apps_table.reloadData()
        self._update_status_label()
        manager.set_status_callback(self._on_status_change_background_thread)
        manager.set_health_callback(self._on_health_change_background_thread)
        self.core.set_ui_hooks(self._run_on_main_thread, self._refresh_from_core)
        if self.core.health_auto_enabled:
            manager.start_health_checks(self.core.health_interval)
        if self.core.mcp_enabled:
            self._start_mcp_host(quiet=True)
        return self

    def _save_state(self):
        self.core.try_save()

    def _run_on_main_thread(self, fn):
        done = threading.Event()
        box = {}

        def task():
            try:
                box["value"] = fn()
            except BaseException as exc:
                box["error"] = exc
            finally:
                done.set()

        with self._main_thread_tasks_lock:
            self._main_thread_tasks.append(task)
        self.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
            "drainMainThreadTasks:", None, False, [NSDefaultRunLoopMode]
        )
        if not done.wait(MAIN_THREAD_TIMEOUT):
            raise ControllerError(
                "the ProxyLoader window is busy (a dialog is probably open); try again once it's closed"
            )
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def drainMainThreadTasks_(self, sender):
        while True:
            with self._main_thread_tasks_lock:
                if not self._main_thread_tasks:
                    return
                task = self._main_thread_tasks.popleft()
            task()

    def _refresh_from_core(self):
        self.table.reloadData()
        self.apps_table.reloadData()
        self.port_field.setStringValue_(str(self.manager.bind_port))
        self.chain_field.setStringValue_(",".join(str(i) for i in self.manager.chain))
        self.health_auto_checkbox.setState_(1 if self.core.health_auto_enabled else 0)
        self.health_interval_field.setStringValue_(str(int(self.core.health_interval)))
        self._apply_mode_to_ui()
        self._update_status_label()

    def windowWillClose_(self, notification):
        self._save_state()
        self.core.cleanup_finished_firefox_profiles()

    def cleanupProfilesClicked_(self, sender):
        result = self.core.cleanup_firefox_profiles()
        removed = result["removed"]
        skipped = result["skipped_in_use"]

        if not removed and not skipped:
            message = "No leftover Firefox profile folders found — already clean."
        else:
            message = f"Removed {len(removed)} leftover Firefox profile folder(s)."
            if skipped:
                message += (
                    f"\n\n{len(skipped)} still look like they're in use by a "
                    f"running Firefox (a lock file is present) and were left "
                    f"alone — quit that Firefox and click Clean up again to "
                    f"catch it too."
                )
        self._show_alert("Clean up Firefox profiles", message)

    def _build_window(self):
        style_mask = (
            NSTitledWindowMask
            | NSClosableWindowMask
            | NSMiniaturizableWindowMask
            | NSResizableWindowMask
        )
        rect = NSMakeRect(200, 120, WINDOW_WIDTH, WINDOW_HEIGHT)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style_mask, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("proxyloader")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setMinSize_((360, 600))
        self.window.setDelegate_(self)

        content_bounds = NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
        glass = NSVisualEffectView.alloc().initWithFrame_(content_bounds)
        glass.setMaterial_(NSVisualEffectMaterialHUDWindow)
        glass.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        glass.setState_(NSVisualEffectStateActive)
        glass.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.window.setContentView_(glass)
        self.glass = glass

        self._add_proxy_table()
        self._add_import_button()
        self._add_paste_button()
        self._add_remove_button()
        self._add_sort_button()
        self._add_port_field()
        self._add_health_interval_row()
        self._add_mode_toggle()
        self._add_chain_row()
        self._add_chain_hint()
        self._add_apps_section()
        self._add_start_stop_button()
        self._add_cleanup_button()
        self._add_mcp_button()
        self._add_status_label()

        self.window.center()

    def _add_proxy_table(self):
        table_frame = NSMakeRect(20, 520, WINDOW_WIDTH - 40, 260)
        scroll_view = NSScrollView.alloc().initWithFrame_(table_frame)
        scroll_view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll_view.setDrawsBackground_(False)

        table = NSTableView.alloc().initWithFrame_(table_frame)
        table.setBackgroundColor_(NSColor.clearColor())
        table.setAllowsMultipleSelection_(True)
        column = NSTableColumn.alloc().initWithIdentifier_("proxy")
        column.setWidth_(table_frame.size.width - 145)
        column.headerCell().setStringValue_("Loaded proxies")
        table.addTableColumn_(column)

        health_column = NSTableColumn.alloc().initWithIdentifier_("health")
        health_column.setWidth_(125)
        health_column.headerCell().setStringValue_("Status")
        table.addTableColumn_(health_column)

        table.setDataSource_(self.data_source)
        table.setDelegate_(self)
        table.setTarget_(self)
        table.setAction_("tableRowClicked:")

        scroll_view.setDocumentView_(table)
        self.glass.addSubview_(scroll_view)
        self.table = table

    def tableView_willDisplayCell_forTableColumn_row_(self, table_view, cell, column, row):
        if table_view is not self.table:
            return
        entries = self.manager.entries
        if row >= len(entries):
            return
        identifier = column.identifier()
        if identifier == "proxy":
            chain = self.manager.chain
            is_active = (row in chain) if chain else (row == self.manager.active_index)
            if is_active:
                cell.setFont_(NSFont.boldSystemFontOfSize_(13))
                cell.setTextColor_(NSColor.controlAccentColor())
            else:
                cell.setFont_(NSFont.systemFontOfSize_(13))
                cell.setTextColor_(NSColor.labelColor())
        elif identifier == "health":
            status = self.manager.health.get(row)
            color = NSColor.tertiaryLabelColor()
            if status is not None:
                if status.status == STATUS_UP:
                    color = NSColor.systemGreenColor()
                elif status.status == STATUS_DOWN:
                    color = NSColor.systemRedColor()
                elif status.status == STATUS_CHECKING:
                    color = NSColor.systemGrayColor()
            cell.setFont_(NSFont.systemFontOfSize_(13))
            cell.setTextColor_(color)

    def tableViewSelectionDidChange_(self, notification):
        self._update_status_label()

    def tableRowClicked_(self, sender):
        row = self.table.clickedRow()
        if row < 0:
            return
        if self.core.mode == "selected":
            app_row = self.apps_table.selectedRow()
            if app_row >= 0 and app_row < len(self.core.app_targets):
                self.core.app_targets[app_row].proxy_index = row
                self.apps_table.reloadData()
                self._update_status_label()
                self._save_state()
                return
        self.manager.set_active(row)
        self._update_status_label()
        self._save_state()

    def _add_import_button(self):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(20, 480, 130, 32))
        button.setTitle_("Import list")
        button.setBezelStyle_(1)
        button.setTarget_(self)
        button.setAction_("importClicked:")
        self.glass.addSubview_(button)

    def _choose_import_mode(self):
        if not self.manager.entries:
            return "append"
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Add to the existing list, or replace it?")
        alert.setInformativeText_(
            f"{len(self.manager.entries)} proxy(ies) are already loaded."
        )
        alert.addButtonWithTitle_("Append")
        alert.addButtonWithTitle_("Replace List")
        alert.addButtonWithTitle_("Cancel")
        response = alert.runModal()
        if response == NSALERT_FIRST_BUTTON_RETURN:
            return "append"
        if response == NSALERT_FIRST_BUTTON_RETURN + 1:
            return "replace"
        return None

    def _import_summary(self, result, candidates=None):
        message = f"Added {result.added} proxy(ies)."
        if result.duplicates:
            message += f" {result.duplicates} duplicate(s) were skipped."
        if candidates is not None:
            unrecognized = candidates - result.added - result.duplicates
            if unrecognized > 0:
                message += (
                    f" {unrecognized} line(s) didn't match a recognized "
                    f"format and were skipped."
                )
        return message

    def importClicked_(self, sender):
        panel = NSOpenPanel.openPanel()
        panel.setAllowsMultipleSelection_(False)
        panel.setCanChooseDirectories_(False)
        panel.setCanChooseFiles_(True)

        if panel.runModal() != 1:
            return
        urls = panel.URLs()
        if not len(urls):
            return

        mode = self._choose_import_mode()
        if mode is None:
            return

        path = urls[0].path()
        result = self.manager.import_file(path, replace=(mode == "replace"))
        self.core.last_import_path = path
        self.table.reloadData()
        self._update_status_label()
        self.manager.probe_now()
        self._save_state()
        self._show_alert("Import list", self._import_summary(result))

    def _add_paste_button(self):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(160, 440, 130, 32))
        button.setTitle_("Paste list")
        button.setBezelStyle_(1)
        button.setTarget_(self)
        button.setAction_("pasteClicked:")
        self.glass.addSubview_(button)
        self.paste_button = button

    def pasteClicked_(self, sender):
        text_frame = NSMakeRect(0, 26, 380, 194)
        text_view = NSTextView.alloc().initWithFrame_(text_frame)
        text_view.setVerticallyResizable_(True)
        text_view.setHorizontallyResizable_(False)
        text_view.setFont_(NSFont.systemFontOfSize_(12))

        scroll_view = NSScrollView.alloc().initWithFrame_(text_frame)
        scroll_view.setHasVerticalScroller_(True)
        scroll_view.setBorderType_(1)
        scroll_view.setDocumentView_(text_view)

        accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 380, 220))
        accessory.addSubview_(scroll_view)

        replace_checkbox = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 380, 22))
        replace_checkbox.setButtonType_(NSSwitchButton)
        replace_checkbox.setTitle_("Replace current list instead of appending")
        replace_checkbox.setState_(0)
        replace_checkbox.setEnabled_(bool(self.manager.entries))
        accessory.addSubview_(replace_checkbox)

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Paste a list of proxies")
        alert.setInformativeText_(
            "One per line, or separated by ';'. host:port, host:port:user:pass, "
            "user:pass@host:port, scheme:// URLs, and comma/tab/pipe-separated "
            "rows (IPRoyal, Bright Data, and similar export formats) are all "
            "recognized."
        )
        alert.setAccessoryView_(accessory)
        alert.addButtonWithTitle_("Import")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != NSALERT_FIRST_BUTTON_RETURN:
            return

        text = text_view.string()
        if not text or not text.strip():
            return

        replace = bool(replace_checkbox.state())
        candidates = count_candidate_lines(text)
        result = self.manager.import_text(text, replace=replace)
        self.table.reloadData()
        self._update_status_label()
        self.manager.probe_now()
        self._save_state()

        self._show_alert("Paste list", self._import_summary(result, candidates))

    def _add_remove_button(self):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(20, 440, 130, 32))
        button.setTitle_("Remove selected")
        button.setBezelStyle_(1)
        button.setTarget_(self)
        button.setAction_("removeClicked:")
        button.setEnabled_(False)
        self.glass.addSubview_(button)
        self.remove_button = button

    def removeClicked_(self, sender):
        if self._is_running():
            return
        rows = sorted(list(self.table.selectedRowIndexes()), reverse=True)
        rows = [row for row in rows if 0 <= row < len(self.manager.entries)]
        if not rows:
            return

        if len(rows) == 1:
            entry = self.manager.entries[rows[0]]
            title = "Remove this proxy?"
            detail = (
                f"{entry.label()} will be removed from the list. Any apps "
                f"pinned to it will fall back to the active proxy."
            )
        else:
            title = f"Remove {len(rows)} proxies?"
            detail = (
                f"{len(rows)} proxies will be removed from the list. Any apps "
                f"pinned to them will fall back to the active proxy."
            )

        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(detail)
        alert.addButtonWithTitle_("Remove")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != NSALERT_FIRST_BUTTON_RETURN:
            return

        for row in rows:
            if not self.manager.remove_at(row):
                continue
            for target in self.core.app_targets:
                if target.proxy_index is None:
                    continue
                if target.proxy_index == row:
                    target.proxy_index = None
                elif target.proxy_index > row:
                    target.proxy_index -= 1

        self.table.reloadData()
        self.apps_table.reloadData()
        self._update_status_label()
        self._save_state()

    def _add_sort_button(self):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(300, 440, 130, 32))
        button.setTitle_("Sort by latency")
        button.setBezelStyle_(1)
        button.setTarget_(self)
        button.setAction_("sortByLatencyClicked:")
        self.glass.addSubview_(button)
        self.sort_button = button

    def sortByLatencyClicked_(self, sender):
        mapping = self.manager.sort_by_latency()
        for target in self.core.app_targets:
            if target.proxy_index is not None:
                target.proxy_index = mapping.get(target.proxy_index)
        self.table.reloadData()
        self.apps_table.reloadData()
        self._update_status_label()
        self._save_state()

    def _add_port_field(self):
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(160, 486, 40, 20))
        label.setStringValue_("Port")
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setTextColor_(NSColor.secondaryLabelColor())
        self.glass.addSubview_(label)

        self.port_field = NSTextField.alloc().initWithFrame_(NSMakeRect(205, 480, 70, 24))
        self.port_field.setStringValue_(str(self.manager.bind_port))
        self.port_field.setDelegate_(self)
        self.glass.addSubview_(self.port_field)

        check_button = NSButton.alloc().initWithFrame_(NSMakeRect(300, 480, 130, 24))
        check_button.setTitle_("Check now")
        check_button.setBezelStyle_(1)
        check_button.setTarget_(self)
        check_button.setAction_("checkNowClicked:")
        self.glass.addSubview_(check_button)

    def checkNowClicked_(self, sender):
        self.manager.probe_now()

    def _add_health_interval_row(self):
        checkbox = NSButton.alloc().initWithFrame_(NSMakeRect(20, 396, 160, 24))
        checkbox.setButtonType_(NSSwitchButton)
        checkbox.setTitle_("Auto-check health")
        checkbox.setState_(1 if self.core.health_auto_enabled else 0)
        checkbox.setTarget_(self)
        checkbox.setAction_("autoHealthToggled:")
        self.glass.addSubview_(checkbox)
        self.health_auto_checkbox = checkbox

        every_label = NSTextField.alloc().initWithFrame_(NSMakeRect(185, 400, 35, 20))
        every_label.setStringValue_("every")
        every_label.setBezeled_(False)
        every_label.setDrawsBackground_(False)
        every_label.setEditable_(False)
        every_label.setSelectable_(False)
        every_label.setTextColor_(NSColor.secondaryLabelColor())
        self.glass.addSubview_(every_label)

        self.health_interval_field = NSTextField.alloc().initWithFrame_(NSMakeRect(222, 396, 45, 24))
        self.health_interval_field.setStringValue_(str(int(self.core.health_interval)))
        self.health_interval_field.setDelegate_(self)
        self.glass.addSubview_(self.health_interval_field)

        seconds_label = NSTextField.alloc().initWithFrame_(NSMakeRect(270, 400, 20, 20))
        seconds_label.setStringValue_("s")
        seconds_label.setBezeled_(False)
        seconds_label.setDrawsBackground_(False)
        seconds_label.setEditable_(False)
        seconds_label.setSelectable_(False)
        seconds_label.setTextColor_(NSColor.secondaryLabelColor())
        self.glass.addSubview_(seconds_label)

    def autoHealthToggled_(self, sender):
        self.core.health_auto_enabled = bool(sender.state())
        if self.core.health_auto_enabled:
            self.manager.start_health_checks(self.core.health_interval)
        else:
            self.manager.stop_health_checks()
        self._save_state()

    def controlTextDidEndEditing_(self, notification):
        obj = notification.object()
        if obj is self.port_field:
            if self._flush_port_field():
                self._save_state()
        elif obj is self.health_interval_field:
            if self._flush_health_interval_field():
                self._save_state()

    def _flush_port_field(self) -> bool:
        try:
            port = int(self.port_field.stringValue())
        except ValueError:
            return False
        if not (0 < port < 65536):
            return False
        self.manager.bind_port = port
        return True

    def _flush_health_interval_field(self) -> bool:
        try:
            interval = float(self.health_interval_field.stringValue())
        except ValueError:
            return False
        if interval < 5:
            return False
        self.core.health_interval = interval
        self.manager.set_health_interval(interval)
        return True

    def applicationWillTerminate_(self, notification):
        self._flush_port_field()
        self._flush_health_interval_field()
        self._stop_mcp_host()
        self._save_state()
        self.core.cleanup_finished_firefox_profiles()

    def _add_mode_toggle(self):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(20, 360, 220, 28))
        button.setTitle_("Mode: All processes")
        button.setBezelStyle_(1)
        button.setTarget_(self)
        button.setAction_("toggleMode:")
        self.glass.addSubview_(button)
        self.mode_button = button

    def toggleMode_(self, sender):
        if self._is_running():
            return
        self.core.mode = "selected" if self.core.mode == "all" else "all"
        self._apply_mode_to_ui()
        self._update_status_label()
        self._save_state()

    def _apply_mode_to_ui(self):
        label = "Selected apps" if self.core.mode == "selected" else "All processes"
        self.mode_button.setTitle_(f"Mode: {label}")
        is_selected = self.core.mode == "selected"
        self.apps_scroll.setHidden_(not is_selected)
        self.add_app_button.setHidden_(not is_selected)
        self.unpin_button.setHidden_(not is_selected)

    def _add_chain_row(self):
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 326, 40, 20))
        label.setStringValue_("Chain")
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setTextColor_(NSColor.secondaryLabelColor())
        self.glass.addSubview_(label)

        self.chain_field = NSTextField.alloc().initWithFrame_(NSMakeRect(65, 320, 150, 24))
        self.chain_field.setPlaceholderString_("row order, e.g. 0,2,1")
        if self.manager.chain:
            self.chain_field.setStringValue_(
                ",".join(str(i) for i in self.manager.chain)
            )
        self.glass.addSubview_(self.chain_field)

        set_button = NSButton.alloc().initWithFrame_(NSMakeRect(225, 316, 60, 28))
        set_button.setTitle_("Set")
        set_button.setBezelStyle_(1)
        set_button.setTarget_(self)
        set_button.setAction_("setChainClicked:")
        self.glass.addSubview_(set_button)
        self.chain_set_button = set_button

        clear_button = NSButton.alloc().initWithFrame_(NSMakeRect(290, 316, 70, 28))
        clear_button.setTitle_("Clear")
        clear_button.setBezelStyle_(1)
        clear_button.setTarget_(self)
        clear_button.setAction_("clearChainClicked:")
        self.glass.addSubview_(clear_button)
        self.chain_clear_button = clear_button

    def _add_chain_hint(self):
        hint = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 296, WINDOW_WIDTH - 40, 16))
        hint.setBezeled_(False)
        hint.setDrawsBackground_(False)
        hint.setEditable_(False)
        hint.setSelectable_(False)
        hint.setFont_(NSFont.systemFontOfSize_(11))
        hint.setTextColor_(NSColor.secondaryLabelColor())
        hint.setStringValue_("")
        self.glass.addSubview_(hint)
        self.chain_hint_label = hint

    def setChainClicked_(self, sender):
        if self._is_running():
            return
        raw = str(self.chain_field.stringValue())
        indices = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                indices.append(int(part))
            except ValueError:
                return
        if self.manager.set_chain(indices):
            self.table.reloadData()
            self._update_status_label()
            self._save_state()

    def clearChainClicked_(self, sender):
        if self._is_running():
            return
        self.manager.clear_chain()
        self.chain_field.setStringValue_("")
        self._update_status_label()
        self._save_state()

    def _add_apps_section(self):
        add_button = NSButton.alloc().initWithFrame_(NSMakeRect(20, 260, 140, 28))
        add_button.setTitle_("Add app…")
        add_button.setBezelStyle_(1)
        add_button.setTarget_(self)
        add_button.setAction_("addAppClicked:")
        add_button.setHidden_(True)
        self.glass.addSubview_(add_button)
        self.add_app_button = add_button

        unpin_button = NSButton.alloc().initWithFrame_(NSMakeRect(170, 260, 100, 28))
        unpin_button.setTitle_("Unpin")
        unpin_button.setBezelStyle_(1)
        unpin_button.setTarget_(self)
        unpin_button.setAction_("unpinAppClicked:")
        unpin_button.setHidden_(True)
        unpin_button.setEnabled_(False)
        unpin_button.setToolTip_("Select an app with a pinned proxy to unpin it.")
        self.glass.addSubview_(unpin_button)
        self.unpin_button = unpin_button

        table_frame = NSMakeRect(20, 120, WINDOW_WIDTH - 40, 130)
        scroll_view = NSScrollView.alloc().initWithFrame_(table_frame)
        scroll_view.setAutoresizingMask_(NSViewWidthSizable)
        scroll_view.setDrawsBackground_(False)
        scroll_view.setHidden_(True)

        table = NSTableView.alloc().initWithFrame_(table_frame)
        table.setBackgroundColor_(NSColor.clearColor())
        column = NSTableColumn.alloc().initWithIdentifier_("app")
        column.setWidth_(table_frame.size.width - 20)
        column.headerCell().setStringValue_("Apps — select, then click a proxy above to pin it")
        table.addTableColumn_(column)
        table.setDataSource_(self.apps_data_source)
        table.setDelegate_(self)

        scroll_view.setDocumentView_(table)
        self.glass.addSubview_(scroll_view)
        self.apps_table = table
        self.apps_scroll = scroll_view

    def addAppClicked_(self, sender):
        panel = NSOpenPanel.openPanel()
        panel.setAllowsMultipleSelection_(True)
        panel.setCanChooseDirectories_(False)
        panel.setCanChooseFiles_(True)
        panel.setAllowedFileTypes_(["app"])
        panel.setDirectoryURL_(NSURL.fileURLWithPath_("/Applications"))

        if panel.runModal() == 1:
            for url in panel.URLs():
                target = app_bundle.resolve_app_bundle(url.path())
                if target:
                    self.core.app_targets.append(target)
            self.apps_table.reloadData()
            self._save_state()

    def unpinAppClicked_(self, sender):
        row = self.apps_table.selectedRow()
        if row < 0 or row >= len(self.core.app_targets):
            return
        target = self.core.app_targets[row]
        if target.proxy_index is None:
            return
        target.proxy_index = None
        self.apps_table.reloadData()
        self._update_status_label()
        self._save_state()

    def _add_start_stop_button(self):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(WINDOW_WIDTH - 150, 70, 130, 32))
        button.setTitle_("Start")
        button.setBezelStyle_(1)
        button.setTarget_(self)
        button.setAction_("toggleServer:")
        self.glass.addSubview_(button)
        self.start_stop_button = button

    def _add_cleanup_button(self):
        # Not mode/state-gated on purpose: leftover Firefox profile dirs can
        # exist regardless of what proxyloader is currently doing (they're
        # filesystem debris from past "selected apps" sessions, possibly
        # from a previous launch of the app entirely), so there's no reason
        # to disable this while the server's running or in "All processes"
        # mode.
        button = NSButton.alloc().initWithFrame_(NSMakeRect(20, 70, 160, 32))
        button.setTitle_("Clean up profiles")
        button.setBezelStyle_(1)
        button.setTarget_(self)
        button.setAction_("cleanupProfilesClicked:")
        self.glass.addSubview_(button)
        self.cleanup_button = button

    def _add_mcp_button(self):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(185, 70, 120, 32))
        button.setTitle_("LLM access: Off")
        button.setBezelStyle_(1)
        button.setTarget_(self)
        button.setAction_("mcpButtonClicked:")
        button.setToolTip_("Let an LLM client (Claude, Cursor, …) manage proxies over MCP on localhost.")
        self.glass.addSubview_(button)
        self.mcp_button = button

    def _update_mcp_button(self):
        running = self.mcp_host is not None and self.mcp_host.running
        self.mcp_button.setTitle_("LLM access: On" if running else "LLM access: Off")

    def _start_mcp_host(self, quiet=False):
        try:
            from mcp_server.http_host import McpHttpHost
            from mcp_server.server import build_server

            self._mcp_token = self.core.secrets.mcp_token()
            host = McpHttpHost(
                build_server(self.core),
                lambda: self._mcp_token,
                host="127.0.0.1",
                port=self.core.mcp_port,
            )
            host.start()
        except Exception as exc:
            self.mcp_host = None
            self._update_mcp_button()
            if not quiet:
                self._show_alert("Couldn't turn on LLM access", str(exc))
            return False
        self.mcp_host = host
        self._update_mcp_button()
        return True

    def _stop_mcp_host(self):
        if self.mcp_host is not None:
            self.mcp_host.stop()
        self.mcp_host = None
        self._update_mcp_button()

    def _copy_mcp_config(self):
        from mcp_server.http_host import client_config

        config = json.dumps(client_config(self.mcp_host.url, self._mcp_token), indent=2)
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(config, NSPasteboardTypeString)

    def mcpButtonClicked_(self, sender):
        if self.mcp_host is None or not self.mcp_host.running:
            if not self._start_mcp_host():
                return
            self.core.mcp_enabled = True
            self._save_state()
            self._copy_mcp_config()
            self._show_alert(
                "LLM access is on",
                f"MCP server listening on {self.mcp_host.url} (this Mac only).\n\n"
                f"A client config with the access token was copied to the clipboard. "
                f"Paste it into your MCP client's settings. Anyone with the token can "
                f"control your proxies, so treat it like a password.",
            )
            return

        alert = NSAlert.alloc().init()
        alert.setMessageText_("LLM access")
        alert.setInformativeText_(f"MCP server running on {self.mcp_host.url}.")
        alert.addButtonWithTitle_("Copy config")
        alert.addButtonWithTitle_("Turn off")
        alert.addButtonWithTitle_("New token")
        alert.addButtonWithTitle_("Cancel")
        choice = alert.runModal()
        if choice == NSALERT_FIRST_BUTTON_RETURN:
            self._copy_mcp_config()
        elif choice == NSALERT_SECOND_BUTTON_RETURN:
            self._stop_mcp_host()
            self.core.mcp_enabled = False
            self._save_state()
        elif choice == NSALERT_THIRD_BUTTON_RETURN:
            try:
                self._mcp_token = self.core.secrets.rotate_mcp_token()
            except Exception as exc:
                self._show_alert("Couldn't make a new token", str(exc))
                return
            self._copy_mcp_config()
            self._show_alert(
                "New token",
                "Old token revoked. The updated client config was copied to the clipboard.",
            )

    def toggleServer_(self, sender):
        if self.core.mode == "all":
            if self.manager.running:
                self.manager.stop()
                try:
                    if self.core.system_proxy_enabled:
                        system_proxy.disable_system_socks_proxy()
                    self.core.system_proxy_enabled = False
                except Exception as exc:
                    self._show_alert(
                        "Proxy stopped, but system proxy wasn't reverted",
                        f"The SOCKS server stopped, but turning off the system-wide "
                        f"proxy setting failed:\n{exc}\n\n"
                        f"You may need to turn it off manually in Network settings.",
                    )
            else:
                try:
                    port = int(self.port_field.stringValue())
                except ValueError:
                    port = self.manager.bind_port
                try:
                    self.manager.start(bind_port=port)
                except ProxyManagerError as exc:
                    self._show_alert("Couldn't start the proxy server", str(exc))
                    self._update_status_label()
                    return
                self._save_state()  # bind_port may have just changed
                try:
                    system_proxy.enable_system_socks_proxy(self.manager.bind_host, port)
                    self.core.system_proxy_enabled = True
                except Exception as exc:
                    self._show_alert(
                        "Proxy running, but system proxy wasn't enabled",
                        f"The SOCKS server is running on port {port}, but enabling the "
                        f"system-wide proxy setting failed:\n{exc}\n\n"
                        f"Traffic won't be routed unless you set the proxy manually "
                        f"or use per-app mode.",
                    )
        else:
            if self.core.selected_mode_active:
                self.core.stop_app_proxies()
            else:
                failures = self.core.launch_apps()["failures"]
                self.core.selected_mode_active = True
                if failures:
                    self._show_alert(
                        "Some apps didn't get a proxy",
                        "\n".join(failures),
                    )

        self._update_status_label()

    def _show_alert(self, title, message):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.runModal()

    def _is_running(self):
        return self.manager.running or self.core.selected_mode_active

    def _add_status_label(self):
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 10, WINDOW_WIDTH - 40, 50))
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_(12))
        label.setTextColor_(NSColor.secondaryLabelColor())
        self.glass.addSubview_(label)
        self.status_label = label
        self._update_status_label()

    def _update_status_label(self):
        chain_entries = self.manager.active_chain()
        if len(chain_entries) > 1:
            active_text = " → ".join(e.label() for e in chain_entries)
        elif chain_entries:
            active_text = chain_entries[0].label()
        else:
            active_text = "no active proxy"
        is_running = self._is_running()
        state_text = "running" if is_running else "stopped"

        if self.core.mode == "all":
            mode_text = f"all processes — forwarding via {active_text}"
        else:
            pinned_count = sum(1 for t in self.core.app_targets if t.proxy_index is not None)
            unpinned_count = len(self.core.app_targets) - pinned_count
            mode_text = f"{pinned_count} app(s) pinned, {unpinned_count} using active ({active_text})"

        self.status_label.setStringValue_(f"{state_text}\nmode: {mode_text}")
        self.start_stop_button.setTitle_("Stop" if is_running else "Start")
        self.mode_button.setEnabled_(not is_running)
        self.mode_button.setToolTip_(
            "Stop the proxy to switch modes." if is_running else ""
        )

        chain_reason = (
            "Stop the proxy to change the chain." if is_running else ""
        )
        self.chain_set_button.setEnabled_(not is_running)
        self.chain_set_button.setToolTip_(chain_reason)
        self.chain_clear_button.setEnabled_(not is_running)
        self.chain_clear_button.setToolTip_(chain_reason)
        self.chain_field.setEnabled_(not is_running)
        self.chain_field.setToolTip_(chain_reason)
        self.chain_hint_label.setStringValue_(chain_reason)

        has_selection = self.table.selectedRowIndexes().count() > 0
        self.remove_button.setEnabled_(not is_running and has_selection)

        app_row = self.apps_table.selectedRow()
        has_pinned_selection = (
            0 <= app_row < len(self.core.app_targets)
            and self.core.app_targets[app_row].proxy_index is not None
        )
        self.unpin_button.setEnabled_(has_pinned_selection)

    def _on_status_change_background_thread(self, is_running):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "onStatusChangeMainThread:", is_running, False
        )

    def onStatusChangeMainThread_(self, is_running):
        self._update_status_label()

    def _on_health_change_background_thread(self):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "onHealthChangeMainThread:", None, False
        )

    def onHealthChangeMainThread_(self, sender):
        self.table.reloadData()

    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
