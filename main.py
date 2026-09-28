"""BadLink — эмулятор плохого канала для отладки на стенде. Запуск: python main.py (нужны права администратора)."""
import ctypes
import sys

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from netem.selftest import run
        sys.exit(run())
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # чёткий текст на мониторах с масштабированием
    except Exception:
        pass
    from netem.gui import main
    main()
