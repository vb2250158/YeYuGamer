import ctypes, json, os, subprocess, sys, time
from ctypes import wintypes
class _BasicLimits(ctypes.Structure):
    _fields_ = [('processTime', ctypes.c_longlong), ('jobTime', ctypes.c_longlong),
        ('LimitFlags', wintypes.DWORD), ('minWorking', ctypes.c_size_t),
        ('maxWorking', ctypes.c_size_t), ('activeLimit', wintypes.DWORD),
        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimits), ('io', ctypes.c_ulonglong * 6),
        ('memory', ctypes.c_size_t * 4)]
class FixtureApi:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'OpenJobObjectW': ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            'GetCurrentProcess': ([], wintypes.HANDLE),
            'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            'DuplicateHandle': ([wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
    def check(self, success, operation):
        if not success:
            raise OSError(operation, ctypes.get_last_error())
    def close(self, handle):
        self.check(self.kernel.CloseHandle(handle), 'fixture.CloseHandle')
api = FixtureApi()
parent_handle = int(sys.argv[1])
if sys.argv[2] != 'no-join':
    job = api.kernel.OpenJobObjectW(1, False, os.environ.pop('PRIVATE_OWNED_JOB'))
    api.check(job, 'fixture.OpenJobObjectW')
    try:
        api.check(api.kernel.AssignProcessToJobObject(job, api.kernel.GetCurrentProcess()), 'fixture.Assign')
    finally:
        api.close(job)
print(json.dumps({'phase': 'bootstrap', 'pid': os.getpid(), 'parentPid': os.getppid()}), flush=True)
if sys.argv[2] == 'no-read':
    time.sleep(20)
if sys.stdin.readline().strip() != 'GO':
    api.close(parent_handle)
    sys.exit(0)
inner = None
if sys.argv[2] == 'nested':
    inner = api.kernel.CreateJobObjectW(None, None)
    api.check(inner, 'fixture.CreateInner')
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x800
    api.check(api.kernel.SetInformationJobObject(inner, 9, ctypes.byref(limits), ctypes.sizeof(limits)), 'fixture.SetInner')
    api.check(api.kernel.AssignProcessToJobObject(inner, api.kernel.GetCurrentProcess()), 'fixture.AssignInner')
children = []
for index in range(2):
    flags = subprocess.CREATE_NO_WINDOW
    if inner and index == 1:
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(20)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True)
    transferred = wintypes.HANDLE()
    api.check(api.kernel.DuplicateHandle(api.kernel.GetCurrentProcess(), int(child._handle),
        parent_handle, ctypes.byref(transferred), 0, False, 2), 'fixture.TransferChildHandle')
    children.append({'pid': child.pid, 'parentPid': os.getpid(), 'handle': transferred.value,
                     'breakaway': bool(inner and index == 1)})
api.close(parent_handle)
if inner:
    api.close(inner)
print(json.dumps({'phase': 'children', 'children': children}), flush=True)
time.sleep(20)
