import ctypes
from ctypes import wintypes
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetTickCount64.restype = ctypes.c_ulonglong
ms = k32.GetTickCount64()
print("uptime: %d days %02d:%02d:%02d" % (ms // 86400000, ms // 3600000 % 24, ms // 60000 % 60, ms // 1000 % 60))
