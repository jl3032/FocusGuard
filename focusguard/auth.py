"""Check a Mac account password with macOS's own login check (PAM), the way the
lock screen does. The password is handed over in memory only: it never appears in a
command line, a file or the log."""
from __future__ import annotations

import ctypes
import ctypes.util

PAM_SUCCESS = 0
PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2


class PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class PamResponse(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]


CONV_FUNC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
                             ctypes.POINTER(ctypes.POINTER(PamMessage)),
                             ctypes.POINTER(ctypes.POINTER(PamResponse)), ctypes.c_void_p)


class PamConv(ctypes.Structure):
    _fields_ = [("conv", CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]


def check_password(user: str, password: str) -> bool:
    if not user or not password:
        return False
    try:
        libpam = ctypes.CDLL(ctypes.util.find_library("pam") or "/usr/lib/libpam.dylib")
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
    except OSError:
        return False
    libc.calloc.restype = ctypes.c_void_p
    libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
    libc.strdup.restype = ctypes.c_void_p
    libc.strdup.argtypes = [ctypes.c_char_p]
    secret = password.encode()

    @CONV_FUNC
    def conv(n, msgs, resp, _data):
        # PAM frees what we return, so it must come from malloc (calloc/strdup).
        arr = libc.calloc(n, ctypes.sizeof(PamResponse))
        replies = ctypes.cast(arr, ctypes.POINTER(PamResponse))
        for i in range(n):
            if msgs[i].contents.msg_style in (PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON):
                replies[i].resp = ctypes.cast(libc.strdup(secret), ctypes.c_char_p)
                replies[i].resp_retcode = 0
        resp[0] = replies
        return PAM_SUCCESS

    handle = ctypes.c_void_p()
    libpam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(PamConv),
                                 ctypes.POINTER(ctypes.c_void_p)]
    libpam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libpam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
    conv_struct = PamConv(conv, None)
    # "chkpasswd" is macOS's service for exactly this: checking a local account password.
    if libpam.pam_start(b"chkpasswd", user.encode(), ctypes.byref(conv_struct),
                        ctypes.byref(handle)) != PAM_SUCCESS:
        return False
    try:
        return libpam.pam_authenticate(handle, 0) == PAM_SUCCESS
    finally:
        libpam.pam_end(handle, 0)
