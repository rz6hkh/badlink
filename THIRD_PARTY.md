# Сторонние компоненты

BadLink распространяется вместе со следующими компонентами без изменений.

| Компонент | Версия | Где лежит | Лицензия | Источник |
|---|---|---|---|---|
| WinDivert (WinDivert.dll, WinDivert64.sys) | 2.2.2 | `windivert/` | LGPLv3 (или GPLv2), текст — `windivert/LICENSE` | https://github.com/basil00/WinDivert |
| iperf3 (iperf3.exe) | 3.21 | `iperf3/` | BSD-3-Clause, текст — `iperf3/LICENSE-iperf3.txt` | https://github.com/esnet/iperf, Windows-сборка https://github.com/ar51an/iperf3-win-builds |
| Cygwin runtime (cygwin1.dll) | 3.6 (из сборки iperf3 3.21) | `iperf3/` | LGPLv3, текст — `iperf3/LICENSE-cygwin-LGPLv3.txt` | https://cygwin.com |
| iperf3 для Windows 7 (iperf3.exe, cygwin1.dll 3.4.10) | 3.21 | `iperf3-win7/` | BSD-3-Clause и LGPLv3, тексты — в той же папке | https://github.com/ar51an/iperf3-win-builds (сборка win7-64Bit) |

WinDivert и Cygwin используются как отдельные динамические библиотеки и могут быть заменены
пользователем на другие совместимые версии (требование LGPL).
