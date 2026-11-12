@echo off
rem Double-click to open the OOT texture tool window (needs Python 3.12 with Pillow, and Python 3.14+ for zstd).
start "" pyw -3.12 "%~dp0texture_gui.pyw" %*
