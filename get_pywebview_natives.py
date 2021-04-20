#!/usr/bin/env python3
import webview
import os.path
import shutil

lib_root = os.path.join(os.path.dirname(webview.__file__), "lib")

files_to_copy = [
    "WebBrowserInterop.x86.dll",
    "WebBrowserInterop.x64.dll",
    "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll"
]

for f in files_to_copy:
    shutil.copy(os.path.join(lib_root, f), ".")
