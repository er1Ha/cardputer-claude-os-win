' run_silent.vbs — Launch claude_pull.py with no console window.
'
' Invoked by Windows Task Scheduler every 60 seconds. Uses pythonw.exe
' (the windowless Python interpreter) so no black flash appears each tick.

Set objShell = CreateObject("WScript.Shell")
Set objFSO   = CreateObject("Scripting.FileSystemObject")

' Locate ourselves so the script works regardless of install path.
strHere   = objFSO.GetParentFolderName(WScript.ScriptFullName)
strPython = "pythonw.exe"
strScript = """" & strHere & "\claude_pull.py"""

objShell.CurrentDirectory = strHere

' 0 = hidden window, False = don't wait for completion
objShell.Run strPython & " " & strScript, 0, False
