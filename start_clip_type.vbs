' Clipboard typer launcher: start clip_type.pyw hidden with the exact
' interpreter that has the dependencies installed (Python 3.10.5).
' ASCII-only on purpose: wscript reads .vbs files as ANSI.
CreateObject("WScript.Shell").Run _
    """D:\python\python3105\pythonw.exe"" ""C:\Users\dell\.zcode\workspace\default\clipboard-typer\clip_type.pyw""", 0, False
