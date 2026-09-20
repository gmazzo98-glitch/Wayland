' Starts the Vienna Crawler Worker in the background (no window). Safe to run twice:
' the worker refuses to start a second copy.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
workerDir = fso.GetParentFolderName(WScript.ScriptFullName)
base = fso.GetParentFolderName(workerDir)
sh.CurrentDirectory = base
sh.Run """" & base & "\node\node.exe"" """ & workerDir & "\worker.mjs""", 0, False
