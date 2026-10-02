# -*- coding: utf-8 -*-
"""
剪贴板拟人打字机 clip_type.pyw
================================
把剪贴板里的内容"像人手敲键盘一样"逐字打到当前焦点位置。

日常使用:
    1. 双击本文件(clip_type.pyw), 脚本在后台常驻(无窗口, 任务管理器里是 pythonw.exe);
    2. 复制任意文本(中英文/数字/标点/emoji 均可);
    3. 鼠标点击目标输入位置(让光标落在那里);
    4. 按 Ctrl+Alt+V, 脚本逐字打出剪贴板内容。

    打字过程中按 Esc 随时中止;
    按 Ctrl+Alt+Q 退出脚本。

自测:
    先打开记事本并点一下输入区, 然后:
        python clip_type.pyw --test
    3 秒后向当前焦点窗口打一段固定测试文本。
"""

import ctypes
import os
import random
import sys
import threading
import time
import traceback

import pyperclip
from pynput import keyboard
from pynput.keyboard import Controller, GlobalHotKeys, Key

# ==================== 可调参数 ====================
# 注意: 不要用 Ctrl+Shift+V, Word/VS Code/Teams 等大量软件把它定义为
# "粘贴为纯文本", 按下瞬间目标软件会先粘贴一遍, 导致内容出现两份。
HOTKEY_TYPE = '<ctrl>+<alt>+v'    # 触发打字的热键
HOTKEY_QUIT = '<ctrl>+<alt>+q'    # 退出脚本的热键

MIN_DELAY = 0.04      # 相邻两字符最小间隔(秒)
MAX_DELAY = 0.12      # 相邻两字符最大间隔(秒)
PAUSE_CHANCE = 0.03   # 每个字符后出现"思考停顿"的概率(0~1)
PAUSE_MIN = 0.3       # 思考停顿最短时长(秒)
PAUSE_MAX = 0.7       # 思考停顿最长时长(秒)
START_DELAY = 0.4     # 按下热键后的缓冲(秒), 等你松开 Ctrl/Shift 再开始

TEST_TEXT = (
    "你好, 世界! Hello World 123.\n"
    "中英文数字标点混合: abc_XYZ_789, 拟人度 100%。\n"
    "换行会被敲成回车, 制表符\t会被敲成 Tab 键。\n"
    "emoji 也能打: 你好 👋 🚀"
)
# =================================================

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'error.log')

# ---- Windows Unicode 注入(绕过输入法, 与 AutoHotkey SendText 同机制) ----
# 不能用 pynput 打普通字符: 它对小写字母/数字发真实键码, 会被中文输入法
# 拦截进拼音组合(Hello -> 饿了咯)。KEYEVENTF_UNICODE 直接产生字符, 不经过
# 任何键盘布局/输入法, 对中英文/emoji 都一致。
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT),
                ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


# 独立 WinDLL 实例: 不能用 ctypes.windll 缓存对象并改其 argtypes,
# 否则会污染 pynput 内部对同一函数对象的原型设置
_SendInput = ctypes.WinDLL('user32').SendInput
_SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
_SendInput.restype = ctypes.c_uint


def _send_units(units):
    """把 UTF-16 码元序列作为 Unicode 按键注入: 一批按下, 一批抬起。"""
    n = len(units)
    down = (_INPUT * n)()
    up = (_INPUT * n)()
    for i, u in enumerate(units):
        down[i].type = up[i].type = INPUT_KEYBOARD
        down[i].u.ki = _KEYBDINPUT(0, u, KEYEVENTF_UNICODE, 0, None)
        up[i].u.ki = _KEYBDINPUT(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
    _SendInput(n, down, ctypes.sizeof(_INPUT))
    _SendInput(n, up, ctypes.sizeof(_INPUT))


def _type_char(ch):
    """注入一个字符; 超出 BMP(emoji 等)自动拆成 UTF-16 代理对。"""
    if ord(ch) > 0xFFFF:
        b = ch.encode('utf-16-le')
        units = [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]
    else:
        units = [ord(ch)]
    _send_units(units)


_typer = Controller()          # 仅用于 Enter/Tab 等真实按键
_stop = threading.Event()      # Esc 中止标志
_typing = threading.Event()    # 正在打字(防止热键重复触发)


def read_clipboard():
    """读剪贴板文本; 剪贴板被占用时稍等重试一次。"""
    for attempt in range(2):
        try:
            return pyperclip.paste() or ''
        except pyperclip.PyperclipWindowsException:
            time.sleep(0.2)
    return ''


def type_text(text):
    """逐字输入 text, 随机节奏模拟真人; 期间检测到 Esc 按下即中止。

    返回 True 表示完整打完, False 表示被中止。
    """
    for ch in text:
        if _stop.is_set():
            return False
        if ch == '\r':
            continue                  # \r\n 里的 \r 直接忽略
        if ch == '\n':
            _typer.tap(Key.enter)
        elif ch == '\t':
            _typer.tap(Key.tab)
        else:
            _type_char(ch)            # 全部走 Unicode 注入, 不受输入法影响
        if random.random() < PAUSE_CHANCE:
            time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    return True


def on_type_hotkey():
    if _typing.is_set():
        return
    _stop.clear()
    text = read_clipboard()
    if not text:
        return
    _typing.set()
    try:
        time.sleep(START_DELAY)
        type_text(text)
    finally:
        _typing.clear()


def on_esc(key):
    if key == Key.esc:
        _stop.set()


def run_daemon():
    esc_listener = keyboard.Listener(on_press=on_esc)
    esc_listener.start()

    hotkeys = GlobalHotKeys({
        HOTKEY_TYPE: on_type_hotkey,
        HOTKEY_QUIT: quit_hotkey,
    })
    hotkeys.start()
    hotkeys.join()


def quit_hotkey():
    os._exit(0)


def run_test():
    print('3 秒后开始向当前焦点窗口输入测试文本, 请立即点一下记事本等输入框...')
    print('打字过程中按 Esc 可中止。')
    esc_listener = keyboard.Listener(on_press=on_esc)
    esc_listener.start()
    time.sleep(3)
    _stop.clear()
    ok = type_text(TEST_TEXT)
    print('打字完成。' if ok else '已按 Esc 中止。')


def _excepthook(exc_type, exc_value, tb):
    text = ''.join(traceback.format_exception(exc_type, exc_value, tb))
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(time.strftime('[%Y-%m-%d %H:%M:%S]\n'))
            f.write(text + '\n' + '-' * 60 + '\n')
    except OSError:
        pass
    try:
        sys.__stderr__.write(text)   # 控制台模式(python/--test)下同时打印
    except Exception:
        pass


def main():
    # pythonw 下没有 stdout/stderr, print 会崩, 重定向到空设备
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    sys.excepthook = _excepthook

    # pynput 监听线程的异常不走 sys.excepthook, 单独兜底写日志
    def _thread_hook(args):
        if args.exc_type is not SystemExit:
            _excepthook(args.exc_type, args.exc_value, args.exc_traceback)
    threading.excepthook = _thread_hook

    if '--test' in sys.argv:
        run_test()
    else:
        run_daemon()


if __name__ == '__main__':
    main()
