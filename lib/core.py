"""
Dwarf - Copyright (C) 2019 Giovanni Rocca (iGio90)

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>
"""
import json

import frida
from PyQt5.QtWidgets import QFileDialog
from event_bus import EventBus
from hexdump import hexdump

from lib import utils
from lib.git import Git
from lib.hook import Hook
from lib.kernel import Kernel
from lib.prefs import Prefs
from lib.scripts_manager import ScriptsManager
from ui.dialog_input import InputDialog
from ui.panel_trace import TraceEvent


class Dwarf(object):
    bus = EventBus()

    def __init__(self, app_window):
        self.app_window = app_window
        self.app = app_window.get_app_instance()

        self.java_available = False
        self.loading_library = False

        # frida device
        self.device = None

        # process
        self.pid = 0
        self.process = None
        self.script = None

        # kernel
        self.kernel = Kernel(self)

        # hooks
        self.hooks = {}
        self.on_loads = {}
        self.java_hooks = {}
        self.temporary_input = ''
        self.native_pending_args = None
        self.java_pending_args = None

        # core utils
        self.prefs = Prefs()
        self.git = Git()
        self.script_manager = ScriptsManager(self)

        self.keystone_installed = False
        try:
            import keystone.keystone_const
            self.keystone_installed = True
        except:
            pass

    def _reinitialize(self):
        self.java_available = False
        self.loading_library = False

        # frida device
        self.device = None

        # process
        self.pid = 0
        self.process = None
        self.script = None

        # hooks
        self.hooks = {}
        self.on_loads = {}
        self.java_hooks = {}
        self.temporary_input = ''
        self.native_pending_args = None
        self.java_pending_args = None

    def device_picked(self, device):
        self.device = device

    def attach(self, pid_or_package, script=None):
        if self.device is None:
            return

        if self.process is not None:
            self.detach()

        try:
            self.process = self.device.attach(pid_or_package)
        except Exception as e:
            utils.show_message_box('Failed to attach to %s' % str(pid_or_package), str(e))
            return
        self.load_script(script)

    def detach(self):
        self.dwarf_api('_detach')
        if self.script is not None:
            self.script.unload()
        if self.process is not None:
            self.process.detach()

    def load_script(self, script=None):
        with open('lib/script.js', 'r') as f:
            s = f.read()
        self.script = self.process.create_script(s)
        self.script.on('message', self.on_message)
        self.script.on('destroyed', self.on_destroyed)
        self.script.load()

        if script is not None:
            self.dwarf_api('evaluateFunction', script)

        self.app_window.on_script_loaded()

    def spawn(self, package, script=None):
        if self.device is None:
            # fallback to usb device
            # can come from -p in args
            self.device = frida.get_usb_device()
            if self.device is None:
                return

        if self.process is not None:
            self.detach()

        try:
            pid = self.device.spawn(package)
            self.process = self.device.attach(pid)
        except Exception as e:
            utils.show_message_box('Failed to spawn to %s' % package, str(e))
            return
        self.load_script(script)
        self.device.resume(pid)

    def on_message(self, message, data):
        if 'payload' not in message:
            print(message)
            return

        what = message['payload']
        parts = what.split(':::')
        if len(parts) < 2:
            print(what)
            return

        cmd = parts[0]
        if cmd == 'backtrace':
            if self.app.get_session_ui() is not None and self.app.get_backtrace_panel() is not None:
                try:
                    self.app.get_backtrace_panel().set_backtrace(json.loads(parts[1]))
                except:
                    pass
        elif cmd == 'enumerate_java_classes_start':
            if self.app.get_java_classes_panel() is not None:
                self.app.get_java_classes_panel().on_enumeration_start()
        elif cmd == 'enumerate_java_classes_match':
            if self.app.get_java_classes_panel() is not None:
                self.app.get_java_classes_panel().on_enumeration_match(parts[1])
        elif cmd == 'enumerate_java_classes_complete':
            self.app_window.get_menu().on_java_classes_enumeration_complete()
            if self.app.get_java_classes_panel() is not None:
                self.app.get_java_classes_panel().on_enumeration_complete()
        elif cmd == 'enumerate_java_methods_complete':
            Dwarf.bus.emit(parts[1], json.loads(parts[2]))
        elif cmd == 'ftrace':
            if self.app.get_ftrace_panel() is not None:
                self.app.get_ftrace_panel().append_data(parts[1])
        elif cmd == 'log':
            self.app.get_log_panel().log(parts[1])
        elif cmd == 'hook_java_callback':
            h = Hook(Hook.HOOK_JAVA)
            h.set_ptr(1)
            h.set_input(parts[1])
            if self.java_pending_args:
                h.set_condition(self.java_pending_args['condition'])
                h.set_logic(self.java_pending_args['logic'])
                self.java_pending_args = None
            self.java_hooks[h.get_input()] = h
            self.app.get_hooks_panel().hook_java_callback(h)
        elif cmd == 'hook_native_callback':
            h = Hook(Hook.HOOK_NATIVE)
            h.set_ptr(int(parts[1], 16))
            h.set_input(self.temporary_input)
            self.temporary_input = ''
            if self.native_pending_args:
                h.set_condition(self.native_pending_args['condition'])
                h.set_logic(self.native_pending_args['logic'])
                self.native_pending_args = None
            self.hooks[h.get_ptr()] = h
            self.app.get_hooks_panel().hook_native_callback(h)
        elif cmd == 'hook_onload_callback':
            h = Hook(Hook.HOOK_ONLOAD)
            h.set_ptr(0)
            h.set_input(parts[1])

            self.on_loads[parts[1]] = h
            if self.app.session_ui is not None and self.app.get_hooks_panel() is not None:
                self.app.get_hooks_panel().hook_onload_callback(h)
        elif cmd == 'memory_scan_match':
            Dwarf.bus.emit(parts[1], parts[2], json.loads(parts[3]))
        elif cmd == 'memory_scan_complete':
            self.app_window.get_menu().on_bytes_search_complete()
            Dwarf.bus.emit(parts[1] + ' complete', 0, 0)
        elif cmd == 'onload_callback':
            self.loading_library = parts[1]
            self.app.get_log_panel().log('hook onload %s @thread := %s' % (
                parts[1], parts[3]))
            self.app.get_hooks_panel().hit_onload(parts[1], parts[2])
        elif cmd == 'set_context':
            data = json.loads(parts[1])
            self.app.get_contexts().append(data)

            if 'context' in data:
                sym = ''
                if 'pc' in data['context']:
                    name = data['ptr']
                    if 'moduleName' in data['symbol']:
                        sym = '(%s - %s)' % (data['symbol']['moduleName'], data['symbol']['name'])
                else:
                    name = data['ptr']
                self.app.get_contexts_panel().add_context(data, library_onload=self.loading_library)
                # check if data['reason'] is 0 (REASON_HOOK)
                if self.loading_library is None and data['reason'] == 0:
                    self.log('hook %s %s @thread := %d' % (name, sym, data['tid']))
                if len(self.app.get_contexts()) > 1 and self.app.get_registers_panel().have_context():
                    return
                self.app.get_session_ui().request_session_ui_focus()
            else:
                self.app.set_arch(data['arch'])
                if self.app.get_arch() == 'arm':
                    self.app.pointer_size = 4
                else:
                    self.app.pointer_size = 8
                self.pid = data['pid']
                self.java_available = data['java']
                self.app.get_log_panel().log('injected into := ' + str(self.pid))
                self.app_window.on_context_info()

            self.app.apply_context(data)
            if self.loading_library is not None:
                self.loading_library = None
        elif cmd == 'set_data':
            key = parts[1]
            if data:
                self.app.get_data_panel().append_data(key, hexdump(data, result='return'))
            else:
                self.app.get_data_panel().append_data(key, str(parts[2]))
        elif cmd == 'tracer':
            panel = self.app.get_trace_panel()
            if panel is not None:
                # safely checked later
                panel.start()

                trace_events_parts = parts[1].split(',')
                while len(trace_events_parts) > 0:
                    t = TraceEvent(trace_events_parts.pop(0),
                                   trace_events_parts.pop(0),
                                   trace_events_parts.pop(0),
                                   trace_events_parts.pop(0))
                    panel.event_queue.append(t)
        elif cmd == 'update_modules':
            self.app.apply_context({'tid': parts[1], 'modules': json.loads(parts[2])})
        elif cmd == 'update_ranges':
            self.app.apply_context({'tid': parts[1], 'ranges': json.loads(parts[2])})
        elif cmd == 'watcher':
            exception = json.loads(parts[1])
            self.log('watcher hit op %s address %s @thread := %s' %
                     (exception['memory']['operation'], exception['memory']['address'], parts[2]))
        elif cmd == 'watcher_added':
            if self.app.get_watchers_panel() is not None:
                self.app.get_watchers_panel().add_watcher_callback(parts[1])
        elif cmd == 'watcher_removed':
            if self.app.get_watchers_panel() is not None:
                self.app.get_watchers_panel().remove_watcher_callback(parts[1])
        else:
            print(what)

    def on_destroyed(self):
        self._reinitialize()

        self.app.get_log_panel().log('detached from %d. script destroyed' % self.pid)
        self.app_window.on_script_destroyed()

    def add_watcher(self, ptr=None):
        if ptr is None:
            ptr, input = InputDialog.input_pointer(self.app)
            if ptr == 0:
                return
        return self.dwarf_api('addWatcher', ptr)

    def dump_memory(self, file_path=None, ptr=0, length=0):
        if ptr == 0:
            ptr, inp = InputDialog.input_pointer(self.app)
        if ptr > 0:
            if length == 0:
                accept, length = InputDialog.input(
                    self.app, hint='insert length', placeholder='1024')
                if not accept:
                    return
                try:
                    if length.startswith('0x'):
                        length = int(length, 16)
                    else:
                        length = int(length)
                except:
                    return
            if file_path is None:
                r = QFileDialog.getSaveFileName(self.app, caption='Save binary dump to file')
                if len(r) == 0 or len(r[0]) == 0:
                    return
                file_path = r[0]
            data = self.read_memory(ptr, length)
            with open(file_path, 'wb') as f:
                f.write(data)

    def dwarf_api(self, api, args=None, tid=0):
        if tid == 0:
            tid = self.app.get_context_tid()
        if args is not None and not isinstance(args, list):
            args = [args]
        if self.script is None:
            return None
        try:
            return self.script.exports.api(tid, api, args)
        except Exception as e:
            self.app.get_log_panel().log(str(e))
            return None

    def hook_java(self, input=None, pending_args=None):
        if input is None or not isinstance(input, str):
            accept, input = InputDialog.input(
                self.app, hint='insert java class or methos',
                placeholder='com.package.class or com.package.class.method')
            if not accept:
                return
        self.java_pending_args = pending_args
        input = input.replace(' ', '')
        self.app.dwarf_api('hookJava', input)

    def hook_native(self, input=None, pending_args=None):
        if input is None or not isinstance(input, str):
            ptr, input = InputDialog.input_pointer(self.app)
        else:
            ptr = utils.parse_ptr(self.app.dwarf_api('evaluatePtr', input))
        if ptr > 0:
            self.temporary_input = input
            self.native_pending_args = pending_args
            self.app.dwarf_api('hookNative', ptr)

    def hook_onload(self, input=None):
        if input is None or not isinstance(input, str):
            accept, input = InputDialog.input(self.app, hint='insert module name', placeholder='libtarget.so')
            if not accept:
                return
            if len(input) == 0:
                return

        if not input.endswith('.so'):
            input += '.so'

        if input in self.app.get_dwarf().on_loads:
            return

        self.dwarf_api('hookOnLoad', input)

    def log(self, what):
        if self.app.get_log_panel() is not None:
            self.app.get_log_panel().log(what)

    def read_memory(self, ptr, len):
        if len > 1024 * 1024:
            position = 0
            next_size = 1024 * 1024
            data = bytearray()
            while True:
                try:
                    data += self.dwarf_api('readBytes', [ptr + position, next_size])
                except:
                    return None
                position += next_size
                diff = len - position
                if diff > 1024 * 1024:
                    next_size = 1024 * 1024
                elif diff > 0:
                    next_size = diff
                else:
                    break
            ret = bytes(data)
            del data
            return ret
        else:
            return self.dwarf_api('readBytes', [ptr, len])

    def remove_watcher(self, ptr):
        return self.dwarf_api('removeWatcher', ptr)

    def get_git(self):
        return self.git

    def get_kernel(self):
        return self.kernel

    def get_loading_library(self):
        return self.loading_library

    def get_prefs(self):
        return self.prefs

    def get_scripts_manager(self):
        return self.script_manager
