"""Allowlisted remote operations and their local approval policy.

All sensitive GUI/ADB and diagnostic mutations require approval in 'approve' mode.
Full Auto is selected only by the device operator in the local tray/console.
"""
from __future__ import annotations

# op: (module name, requires_local_approval, workspace_root_first)
CAPABILITIES = {
    "take_screenshot": ("desktop", True, False),
    "capture_screen": ("desktop", True, False),
    "list_monitors": ("desktop", False, False),
    "record_screen": ("desktop", True, False),
    "list_windows": ("desktop", False, False),
    "capture_window": ("desktop", True, False),
    "focus_window": ("desktop", True, False),
    "resize_window": ("desktop", True, False),
    "inspect_ui": ("desktop", True, False),
    "click_element": ("desktop", True, False),
    "gui_click": ("desktop", True, False),
    "type_text": ("desktop", True, False),
    "send_keys": ("desktop", True, False),
    "android_devices": ("android", False, False),
    "android_screenshot": ("android", True, False),
    "android_tap": ("android", True, False),
    "android_swipe": ("android", True, False),
    "android_launch": ("android", True, False),
    "android_input_text": ("android", True, False),
    "android_logcat": ("android", True, False),
    "android_ui_tree": ("android", True, False),
    "android_profile": ("android", True, False),
    "inspect_process": ("diagnostics", False, False),
    "debug_application": ("diagnostics", False, False),
    "profile_cpu": ("diagnostics", False, False),
    "profile_memory": ("diagnostics", False, False),
    "inspect_gpu": ("diagnostics", False, False),
    "inspect_ports": ("diagnostics", False, False),
    "inspect_requests": ("diagnostics", False, False),
    "test_endpoint": ("diagnostics", True, False),
    "inspect_git": ("diagnostics", False, True),
    "inspect_dependencies": ("diagnostics", False, True),
    "build_project": ("diagnostics", True, True),
    "run_tests": ("diagnostics", True, True),
    "get_crash_logs": ("diagnostics", True, False),
    "capture_test_failure": ("diagnostics", True, False),
}
